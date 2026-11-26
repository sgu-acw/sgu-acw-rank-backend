"""Application settings.

Every knob is an environment variable prefixed with ``SGU_ACW_``; a ``.env``
file next to the working directory is read as well, which is what the local
dev flow uses.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from .student_id import ROOT_STUDENT_ID


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SGU_ACW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///./sgu-acw.db"

    # Sessions
    session_ttl_days: int = 30
    cookie_name: str = "sgu_acw_session"
    # The browser only ever talks to the Pages domain, so the cookie belongs to
    # that host and needs no Domain attribute. Secure stays on in production;
    # local http debugging flips it off.
    cookie_secure: bool = True
    cookie_domain: str | None = None

    # Shared secret with the Pages Function. Requests without it are rejected,
    # which stops anyone from bypassing Cloudflare and hitting the origin
    # directly. Empty disables the check (local development).
    origin_secret: str = ""

    # Shared secret for the editorial generator (pi running on the VPS).
    #
    # A separate secret from ``origin_secret`` on purpose: the generator is a
    # local process, not a Cloudflare edge, so it must not be able to call the
    # public API as if it came through the CDN. This one only unlocks
    # ``/internal/*``. Empty disables the internal routes entirely.
    editorial_secret: str = ""

    # Which model the generator asks for. Recorded on every editorial so a
    # quality regression can be traced back to a model switch.
    editorial_model: str = "deepseek-flash"

    # Web Push VAPID keys. ``vapid-gen`` 生成一次写进 env 就行，别放代码里。
    # 空 = 推送功能降级为不可用，其余一切照常。
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    #: 标准要求的归属标识：mailto: 或 https:。
    vapid_subject: str = "mailto:admin@sgu-acw.zh314.xyz"

    # WebAuthn（通行密钥）的依赖方参数。本地开发换 localhost 需改这三个。
    rp_id: str = "sgu-acw.zh314.xyz"
    rp_name: str = "算法竞赛工作坊"
    rp_origin: str = "https://sgu-acw.zh314.xyz"

    # 两步验证登录票据的签名密钥。密码正确但还没过 TOTP 时下发——
    # 不能直接发卡话，得等二次校验；票据就是这段中间态的凭据。
    # 生产必须换成随机串：拿到密钥=绕过 TOTP。
    ticket_secret: str = "sgu-acw-dev-ticket-secret"

    # First-run bootstrap. When the password is set and no user carries this
    # student id yet, a root account is created. Ignored on every later start.
    #
    # 学号默认是十一个 0——那不是合法学号，所以没人能注册到它，也不会占掉
    # 真实号段。留空就用这个默认值。
    bootstrap_root_student_id: str = ROOT_STUDENT_ID
    bootstrap_root_password: str = ""

    # Scrypt work factor. n must be a power of two; 2**14 keeps a login around
    # 50ms on the target VPS while staying far above the memory a GPU rig would
    # find worthwhile. Tests lower this via SGU_ACW_SCRYPT_N to keep the suite
    # fast; production never overrides it.
    scrypt_n: int = 2**14
    scrypt_r: int = 8
    scrypt_p: int = 1

    # Gate new registrations only. Existing pending accounts still require review.
    registration_review_required: bool = True

    # Invite defaults offered in the admin UI.
    invite_default_max_uses: int = 1
    invite_default_ttl_hours: int = 24

    # 过题数的同步间隔。随「不活跃天数」翻倍，封顶：
    #
    #     间隔 = min(cap, base × 2^不活跃天数)
    #
    # 今天还在刷的人 15 分钟一次，昨天做过的 30 分钟，两天没动静 1 小时，
    # 依此类推，到第 10 天摸到 7 天的上限。
    #
    # 用曲线而不是固定档位：档位之间的跳变很硬（一小时直接跳到六小时），
    # 而活跃度是连续变化的。
    #
    # 七天封顶是折中，再慢就没人看得到变化。真到了谷值还想立刻更新，
    # 成员可以手动刷新（每周七次）。
    sync_interval_base_minutes: int = 15
    sync_interval_cap_minutes: int = 7 * 24 * 60

    # rating 只在打完比赛后变，一天一次足够。
    rating_interval_minutes: int = 24 * 60

    # 验证尝试的最小间隔（秒），以及它的上限。
    #
    # 冷却随失败次数翻倍：10s → 20s → 40s → … 封顶。每次验证都要去平台读
    # 一次资料，连点不仅浪费请求，还可能把平台的 WAF 惹毛——那会连带影响
    # 所有人的抓取，不只是他自己。前端禁用按钮挡不住直接调接口的。
    verify_cooldown_seconds: int = 10
    verify_cooldown_max_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
