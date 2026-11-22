"""下载网络错误的安全诊断：只保留阶段、错误类别和数值代码。

Requests 的原始异常常含临时媒体 URL、签名参数或代理地址；不能直接写入
曲库或聊天。这里不读取异常文本，也不保存请求/响应对象或任何请求字段。
"""
import errno
import math
import ssl

import requests


_STAGES = {'service': '在线解析服务', 'media': '音频下载',
           'cover': '封面下载', 'download': '下载请求', 'network': '网络请求'}
_KINDS = {'certificate', 'tls', 'proxy', 'timeout', 'connection', 'rate_limit',
          'http', 'request', 'configuration', 'permission'}
_CONFIGURATION_ERRORS = (requests.exceptions.InvalidHeader, requests.exceptions.InvalidSchema,
                         requests.exceptions.MissingSchema, requests.exceptions.InvalidURL,
                         requests.exceptions.InvalidJSONError)
_EXCEPTION_TYPES = frozenset((
    'RequestException', 'ConnectionError', 'ProxyError', 'SSLError',
    'SSLCertVerificationError', 'CertificateError', 'Timeout', 'ConnectTimeout',
    'ReadTimeout', 'HTTPError', 'ChunkedEncodingError', 'ContentDecodingError',
    'MaxRetryError', 'NewConnectionError', 'NameResolutionError',
    'ConnectTimeoutError', 'ReadTimeoutError', 'ProtocolError', 'ResponseError',
    'IncompleteRead', 'OSError', 'FileNotFoundError', 'ConnectionRefusedError',
    'ConnectionResetError', 'BrokenPipeError', 'gaierror', 'TimeoutError', 'PermissionError',
    'InvalidHeader', 'InvalidSchema', 'MissingSchema', 'InvalidURL', 'InvalidJSONError',
    'JSONDecodeError',
))


def _exception_chain(error):
    """嵌套原因只看异常对象；从不把 URL 字符串当诊断材料。"""
    pending, seen, result = [error], set(), []
    while pending and len(result) < 16:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        pending.extend((current.__cause__, current.__context__))
        pending.extend(value for value in current.args if isinstance(value, BaseException))
        # urllib3 MaxRetryError 将其底层原因另存在 reason 属性里。
        reason = getattr(current, 'reason', None)
        if isinstance(reason, BaseException):
            pending.append(reason)
    return result


class NetworkFailure(Exception):
    """供下载器保存/显示的错误；硬错误立即停止，临时错误交由批次计数。"""
    def __init__(self, stage, kind, *, exception_types=(), errnos=(),
                 status_code=None, retry_after=None):
        self.stage = stage if stage in _STAGES else 'network'
        self.kind = kind if kind in _KINDS else 'request'
        self.exception_types = tuple(value for value in exception_types if value in _EXCEPTION_TYPES)
        self.errnos = tuple(value for value in errnos if type(value) is int and abs(value) <= 1_000_000)
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        self.retry_after = (retry_after if type(retry_after) in (int, float) and
                            math.isfinite(retry_after) and 0 < retry_after <= 86400 else None)
        self.stop_batch = self.kind in ('certificate', 'tls', 'proxy', 'rate_limit',
                                       'configuration', 'permission')
        self.transient = self.kind in ('timeout', 'connection')
        messages = {'certificate': 'TLS 证书验证失败', 'tls': 'TLS 安全连接失败',
                    'proxy': '代理连接失败', 'timeout': '连接或读取超时',
                    'connection': '网络连接失败', 'rate_limit': '服务暂时限流',
                    'http': 'HTTP 请求失败', 'request': '网络请求失败',
                    'configuration': '请求配置无效', 'permission': '网络访问被系统拒绝'}
        detail = messages[self.kind]
        if self.kind == 'http' and self.status_code is not None:
            detail += f'（HTTP {self.status_code}）'
        elif self.exception_types:
            detail += f'（{self.exception_types[0]}）'
        super().__init__(f'{_STAGES[self.stage]}：{detail}。请检查连接后重试。')

    def diagnostic(self):
        """可直接写入 QA JSON 的白名单字段，不包含原请求/响应。"""
        return {'stage': self.stage, 'kind': self.kind,
                'exception_types': list(self.exception_types), 'errnos': list(self.errnos),
                'status_code': self.status_code, 'stop_batch': self.stop_batch,
                'transient': self.transient}


def network_failure(error, stage='network'):
    if isinstance(error, NetworkFailure):
        return error
    chain = _exception_chain(error)
    status = getattr(getattr(error, 'response', None), 'status_code', None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    # 服务请求格式异常是当前客户端共同配置错误；单曲 CDN URL 异常仍逐曲处理。
    if stage == 'service' and isinstance(error, _CONFIGURATION_ERRORS):
        kind = 'configuration'
    elif any(isinstance(value, OSError) and not isinstance(value, ssl.SSLError) and
             getattr(value, 'errno', None) in (errno.EPERM, errno.EACCES) for value in chain):
        # SSL 自身 errno=1 表示协议/证书错误，不能误归为系统 EPERM。
        kind = 'permission'
    elif isinstance(error, requests.exceptions.SSLError):
        kind = ('certificate' if any(isinstance(value, ssl.SSLCertVerificationError)
                                     for value in chain) else 'tls')
    elif isinstance(error, requests.exceptions.ProxyError):
        kind = 'proxy'
    elif isinstance(error, requests.exceptions.Timeout):
        kind = 'timeout'
    elif isinstance(error, requests.exceptions.ConnectionError):
        kind = 'connection'
    elif isinstance(error, requests.exceptions.HTTPError):
        kind = 'rate_limit' if status == 429 else 'http'
    else:
        kind = 'request'
    types, errnos = [], []
    for value in chain:
        name = type(value).__name__
        if name in _EXCEPTION_TYPES and name not in types:
            types.append(name)
        error_number = getattr(value, 'errno', None)
        if type(error_number) is int and abs(error_number) <= 1_000_000 and error_number not in errnos:
            errnos.append(error_number)
    retry = None
    if kind == 'rate_limit':
        value = getattr(getattr(error, 'response', None), 'headers', {}).get('Retry-After')
        try:
            retry = float(value)
        except (TypeError, ValueError, OverflowError):
            pass
    return NetworkFailure(stage, kind, exception_types=types, errnos=errnos,
                          status_code=status, retry_after=retry)
