"""网易云适配器共用的错误类型和媒体地址检查。"""
from urllib.parse import urlparse


class AuthorizationError(Exception):
    """平台或解析服务拒绝了请求；code 是对方给的状态码（没有则为 None）。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class MusicUnavailable(AuthorizationError):
    """平台明确不提供完整音源；重试解决不了。"""


class IdentityUnavailable(AuthorizationError):
    """没法确认这就是要的那首歌；不用同名结果顶替。"""


def safe_media_url(value):
    """音频和封面只允许从网易自己的 CDN 下载，并且一律走 HTTPS。"""
    if not isinstance(value, str):
        raise MusicUnavailable('没有拿到完整音频的地址。')
    parsed = urlparse(value)
    host = parsed.hostname or ''
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443) or not (
            host.endswith('.music.126.net') or host.endswith('.music.163.com')):
        raise AuthorizationError('音频地址格式异常，没有下载。')
    # 同一资源支持 HTTPS，避免明文传输带时效签名的地址。
    return 'https://' + value.split('://', 1)[1]
