"""工作坊通用 OAuth 2.0 + PKCE 授权服务。

设计目标：
- 为独立部署的校赛客户端、监考后台及后续校内工具提供统一的一键身份认证。
- 严格遵循最小权限与零强耦合：工作坊仅作为身份提供方（IdP）签发一次性授权码
  和短寿命访问令牌，业务侧拿到身份后签发自己的本地会话，互不拖垮。
- 支持公开客户端（强制 PKCE S256 + RFC 8252 loopback 端口通配）和机密客户端
  （client_secret 校验，并支持叠加 PKCE）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, update
from sqlmodel import Session, select

from ..db import get_session
from ..deps import (
    get_current_user,
    get_optional_user,
    require_admin,
    require_origin_secret,
)
from ..models import (
    Account,
    OAuthAccessToken,
    OAuthAuthCode,
    OAuthClient,
    Platform,
    User,
    UserStatus,
    utcnow,
)
from ..security import hash_session_token, new_session_token

router = APIRouter(
    tags=["oauth"],
    dependencies=[Depends(require_origin_secret)],
)

#: 一次性授权码有效期（秒）。
AUTH_CODE_TTL_SECONDS = 60
#: 访问令牌有效期（秒）。仅用于读取用户信息，不做长期凭证。
ACCESS_TOKEN_TTL_SECONDS = 3600

#: 支持的权限范围及其面向用户的中文描述。
SCOPE_CATALOG: dict[str, dict[str, str]] = {
    "profile": {
        "label": "基本身份信息",
        "description": "读取你的学号/账号、姓名与校内/管理员角色标识",
    },
    "campus": {
        "label": "校内院系班级",
        "description": "读取你的入学年级、学院、专业与班级简称",
    },
    "nowcoder": {
        "label": "已绑定牛客账号",
        "description": "读取你在工作坊绑定并验证过的牛客 UID 与昵称",
    },
}

_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_PKCE_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9\-_]{43,128}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _parse_json_list(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if isinstance(x, str) and x.strip()]


def _validate_redirect_uri_entry(uri: str) -> str:
    uri = uri.strip()
    if not uri or len(uri) > 512:
        raise HTTPException(status_code=400, detail="回调地址长度不合法")
    parsed = urlsplit(uri)
    if parsed.fragment:
        raise HTTPException(status_code=400, detail="回调地址不能包含 # 片段")
    if parsed.scheme not in {"https", "http"} and not re.fullmatch(
        r"[a-z][a-z0-9+.-]{1,31}", parsed.scheme
    ):
        raise HTTPException(status_code=400, detail=f"不支持的回调协议：{uri}")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=400,
            detail="非本机回环地址必须使用 https:// 协议",
        )
    if parsed.scheme in {"https", "http"} and not parsed.hostname:
        raise HTTPException(status_code=400, detail=f"回调地址缺少主机名：{uri}")
    return uri


def _match_redirect_uri(registered: list[str], candidate: str) -> bool:
    candidate = candidate.strip()
    if not candidate or len(candidate) > 512:
        return False
    cand = urlsplit(candidate)
    if cand.fragment:
        return False
    if candidate in registered:
        return True
    # RFC 8252 §7.3：loopback 回调若登记时省略端口，允许匹配任意临时本地端口。
    if cand.scheme == "http" and cand.hostname in _LOOPBACK_HOSTS:
        try:
            cand_port = cand.port
        except ValueError:
            return False
        if cand_port is None or not (1 <= cand_port <= 65535):
            return False
        for reg_raw in registered:
            reg = urlsplit(reg_raw)
            if (
                reg.scheme == "http"
                and reg.hostname == cand.hostname
                and reg.port is None
                and reg.path == cand.path
                and reg.query == cand.query
            ):
                return True
    return False


def _parse_requested_scopes(scope_raw: str | None, allowed: list[str]) -> list[str]:
    allowed_set = {s for s in allowed if s in SCOPE_CATALOG}
    if not scope_raw or not scope_raw.strip():
        return ["profile"] if "profile" in allowed_set else sorted(allowed_set)
    parts: list[str] = []
    for token in scope_raw.strip().split():
        if token not in SCOPE_CATALOG or token not in allowed_set:
            raise HTTPException(status_code=400, detail=f"未允许的授权范围：{token}")
        if token not in parts:
            parts.append(token)
    if not parts:
        raise HTTPException(status_code=400, detail="至少需要请求一个授权范围")
    return parts


def _verify_pkce_s256(verifier: str, challenge: str) -> bool:
    if not _PKCE_VERIFIER_RE.fullmatch(verifier):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(computed, challenge)


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    existing = parse_qsl(parts.query, keep_blank_values=True)
    merged = existing + [(k, v) for k, v in params.items() if v is not None]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), ""))


def _validate_authorize_request(
    db: Session,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str | None,
    code_challenge: str | None,
    code_challenge_method: str | None,
) -> tuple[OAuthClient, list[str]]:
    client = db.exec(select(OAuthClient).where(OAuthClient.client_id == client_id)).first()
    if client is None or not client.is_active:
        raise HTTPException(status_code=400, detail="无效或已停用的 OAuth 应用")

    registered_uris = _parse_json_list(client.redirect_uris_json)
    if not _match_redirect_uri(registered_uris, redirect_uri):
        raise HTTPException(status_code=400, detail="回调地址不在应用白名单内")

    scopes = _parse_requested_scopes(scope, _parse_json_list(client.allowed_scopes_json))

    if client.is_public and not code_challenge:
        raise HTTPException(status_code=400, detail="公开客户端必须启用 PKCE（code_challenge）")

    if code_challenge:
        method = (code_challenge_method or "").strip()
        if method != "S256":
            raise HTTPException(status_code=400, detail="仅支持 S256 方式的 PKCE 校验")
        if not _PKCE_CHALLENGE_RE.fullmatch(code_challenge):
            raise HTTPException(status_code=400, detail="code_challenge 格式不合法")

    return client, scopes


def _build_userinfo(user: User, scopes: list[str], db: Session) -> dict:
    payload: dict[str, object] = {
        "sub": str(user.id),
        "id": user.id,
        "student_id": user.student_id,
        "display_name": user.display_name,
        "role": user.role,
        "is_campus_member": user.is_campus_member,
        "is_admin": user.is_admin,
    }
    if "campus" in scopes:
        payload.update(
            {
                "enroll_year": user.enroll_year if user.is_campus_member else None,
                "college_name": user.college_name,
                "major_name": user.major_name,
                "class_label": user.class_label,
            }
        )
    if "nowcoder" in scopes:
        nc_rows = list(
            db.exec(
                select(Account)
                .where(Account.user_id == user.id, Account.platform == Platform.NOWCODER)
                .order_by(Account.verified_at.desc(), Account.id.asc())  # type: ignore[union-attr]
            ).all()
        )
        primary = next((a for a in nc_rows if a.verified_at is not None), None) or (
            nc_rows[0] if nc_rows else None
        )
        payload.update(
            {
                "nowcoder_uid": primary.identifier if primary else None,
                "nowcoder_name": primary.display_name if primary else None,
                "nowcoder_verified": bool(primary and primary.verified_at is not None),
                "nowcoder_accounts": [
                    {
                        "uid": a.identifier,
                        "display_name": a.display_name,
                        "verified": a.verified_at is not None,
                    }
                    for a in nc_rows
                ],
            }
        )
    return payload


def _extract_basic_credentials(auth_header: str | None) -> tuple[str | None, str | None]:
    if not auth_header or not auth_header.startswith("Basic "):
        return None, None
    try:
        raw = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
        cid, csec = raw.split(":", 1)
        return cid, csec
    except (ValueError, UnicodeDecodeError):
        return None, None


# --------------------------------------------------------------------------- #
# 用户授权与令牌交换
# --------------------------------------------------------------------------- #


class ScopeDetail(BaseModel):
    key: str
    label: str
    description: str


class OAuthClientPublicInfo(BaseModel):
    client_id: str
    name: str
    description: str
    is_public: bool
    require_campus: bool


class AuthorizeInfoOut(BaseModel):
    client: OAuthClientPublicInfo
    redirect_uri: str
    scopes: list[ScopeDetail]
    authenticated: bool
    eligible: bool
    ineligible_reason: str | None = None
    user_display_name: str | None = None
    user_signature: str = ""
    user_student_id: str | None = None
    user_role: str | None = None


@router.get("/oauth/authorize/info", response_model=AuthorizeInfoOut)
def authorize_info(
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    scope: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_session),
) -> AuthorizeInfoOut:
    if response_type != "code":
        raise HTTPException(status_code=400, detail="仅支持 response_type=code")
    client, scopes = _validate_authorize_request(
        db,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    eligible = True
    reason: str | None = None
    if user is None:
        eligible = False
        reason = "请先登录工作坊账号"
    elif client.require_campus and not user.is_campus_member:
        eligible = False
        reason = "该应用仅限校内成员使用，当前账号为校外账号"

    return AuthorizeInfoOut(
        client=OAuthClientPublicInfo(
            client_id=client.client_id,
            name=client.name,
            description=client.description,
            is_public=client.is_public,
            require_campus=client.require_campus,
        ),
        redirect_uri=redirect_uri,
        scopes=[
            ScopeDetail(
                key=s,
                label=SCOPE_CATALOG[s]["label"],
                description=SCOPE_CATALOG[s]["description"],
            )
            for s in scopes
        ],
        authenticated=user is not None,
        eligible=eligible,
        ineligible_reason=reason,
        user_display_name=user.display_name if user else None,
        user_signature=user.signature if user else "",
        user_student_id=user.student_id if user else None,
        user_role=user.role if user else None,
    )


class AuthorizeDecisionRequest(BaseModel):
    client_id: str
    redirect_uri: str
    response_type: str = "code"
    scope: str | None = None
    state: str | None = None
    code_challenge: str | None = None
    code_challenge_method: str | None = None
    approve: bool = True


class AuthorizeDecisionOut(BaseModel):
    redirect_url: str
    expires_in: int | None = None


class QRAuthorizeRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    redirect_uri: str = Field(min_length=1, max_length=512)
    scope: str | None = Field(default=None, max_length=128)
    code_challenge: str = Field(min_length=43, max_length=43)


def validate_qr_authorize_request(
    db: Session, body: QRAuthorizeRequest, user: User | None = None
) -> tuple[OAuthClient, list[str]]:
    client, scopes = _validate_authorize_request(
        db,
        client_id=body.client_id,
        redirect_uri=body.redirect_uri,
        scope=body.scope,
        code_challenge=body.code_challenge,
        code_challenge_method="S256",
    )
    redirect = urlsplit(body.redirect_uri)
    if (
        not client.is_public
        or redirect.scheme != "http"
        or redirect.hostname not in _LOOPBACK_HOSTS
    ):
        raise HTTPException(status_code=400, detail="该应用不支持客户端扫码登录")
    if user is not None and client.require_campus and not user.is_campus_member:
        raise HTTPException(status_code=403, detail="该应用仅限校内成员授权")
    return client, scopes


def issue_authorization_code(
    db: Session,
    *,
    client: OAuthClient,
    user: User,
    scopes: list[str],
    redirect_uri: str,
    code_challenge: str | None,
    code_challenge_method: str | None,
) -> str:
    now = utcnow()
    db.exec(delete(OAuthAuthCode).where(OAuthAuthCode.expires_at < now))  # type: ignore[arg-type]
    raw_code = new_session_token()
    assert user.id is not None
    db.add(
        OAuthAuthCode(
            code_hash=hash_session_token(raw_code),
            client_id=client.client_id,
            user_id=user.id,
            redirect_uri=redirect_uri.strip(),
            scope=" ".join(scopes),
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method if code_challenge else None,
            expires_at=now + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
            created_at=now,
        )
    )
    return raw_code


@router.post("/oauth/authorize", response_model=AuthorizeDecisionOut)
def authorize_decision(
    body: AuthorizeDecisionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AuthorizeDecisionOut:
    if body.response_type != "code":
        raise HTTPException(status_code=400, detail="仅支持 response_type=code")

    client, scopes = _validate_authorize_request(
        db,
        client_id=body.client_id,
        redirect_uri=body.redirect_uri,
        scope=body.scope,
        code_challenge=body.code_challenge,
        code_challenge_method=body.code_challenge_method,
    )

    if not body.approve:
        params: dict[str, str] = {"error": "access_denied"}
        if body.state:
            params["state"] = body.state
        return AuthorizeDecisionOut(
            redirect_url=_append_query_params(body.redirect_uri, params),
            expires_in=None,
        )

    if client.require_campus and not user.is_campus_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该应用仅限校内成员授权",
        )

    raw_code = issue_authorization_code(
        db,
        client=client,
        user=user,
        scopes=scopes,
        redirect_uri=body.redirect_uri,
        code_challenge=body.code_challenge,
        code_challenge_method=body.code_challenge_method,
    )
    db.commit()

    params = {"code": raw_code}
    if body.state:
        params["state"] = body.state
    return AuthorizeDecisionOut(
        redirect_url=_append_query_params(body.redirect_uri, params),
        expires_in=AUTH_CODE_TTL_SECONDS,
    )


@router.post("/oauth/token")
async def exchange_token(
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    """用一次性授权码换取短寿命访问令牌与已核验的用户资料。

    同时兼容 ``application/x-www-form-urlencoded``（标准 OAuth 2.0）和
    ``application/json``，并支持 HTTP Basic Auth 传递客户端凭据。
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            raw_body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="无效的 JSON 请求体") from exc
        if not isinstance(raw_body, dict):
            raise HTTPException(status_code=400, detail="请求体必须为对象")
        data = {str(k): ("" if v is None else str(v)) for k, v in raw_body.items()}
    else:
        form = await request.form()
        data = {str(k): str(v) for k, v in form.items()}

    basic_id, basic_secret = _extract_basic_credentials(request.headers.get("authorization"))
    grant_type = data.get("grant_type", "")
    client_id = data.get("client_id") or basic_id or ""
    client_secret = data.get("client_secret") or basic_secret or ""
    code = data.get("code", "")
    redirect_uri = data.get("redirect_uri", "").strip()
    code_verifier = data.get("code_verifier", "")

    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="仅支持 grant_type=authorization_code")
    if not client_id or not code or not redirect_uri:
        raise HTTPException(status_code=400, detail="缺少必要参数（client_id、code、redirect_uri）")

    client = db.exec(select(OAuthClient).where(OAuthClient.client_id == client_id)).first()
    if client is None or not client.is_active:
        raise HTTPException(status_code=401, detail="无效的客户端凭据")

    if not client.is_public and (
        not client_secret
        or not client.client_secret_hash
        or not hmac.compare_digest(hash_session_token(client_secret), client.client_secret_hash)
    ):
        raise HTTPException(status_code=401, detail="客户端密钥错误")

    now = utcnow()
    code_row = db.exec(
        select(OAuthAuthCode).where(OAuthAuthCode.code_hash == hash_session_token(code))
    ).first()
    if code_row is None:
        raise HTTPException(status_code=400, detail="授权码无效或已过期")
    if code_row.client_id != client.client_id or code_row.redirect_uri != redirect_uri:
        raise HTTPException(status_code=400, detail="授权码与客户端或回调地址不匹配")
    if code_row.used_at is not None or now >= code_row.expires_at:
        raise HTTPException(status_code=400, detail="授权码已失效或已使用")

    if code_row.code_challenge and (
        not code_verifier or not _verify_pkce_s256(code_verifier, code_row.code_challenge)
    ):
        raise HTTPException(status_code=400, detail="PKCE 校验失败")

    # 原子标记已使用，防止并发重放同一个授权码。
    consumed = db.exec(
        update(OAuthAuthCode)
        .where(
            OAuthAuthCode.id == code_row.id,
            OAuthAuthCode.used_at.is_(None),  # type: ignore[union-attr]
        )
        .values(used_at=now)
    )
    if consumed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=400, detail="授权码已被使用")

    user = db.get(User, code_row.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        db.commit()
        raise HTTPException(status_code=403, detail="用户账号不可用")
    if client.require_campus and not user.is_campus_member:
        db.commit()
        raise HTTPException(status_code=403, detail="该应用仅限校内成员使用")

    # 清理过期访问令牌并签发新的短寿命访问令牌。
    db.exec(delete(OAuthAccessToken).where(OAuthAccessToken.expires_at < now))  # type: ignore[arg-type]
    raw_token = new_session_token()
    db.add(
        OAuthAccessToken(
            token_hash=hash_session_token(raw_token),
            client_id=client.client_id,
            user_id=user.id,  # type: ignore[arg-type]
            scope=code_row.scope,
            expires_at=now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS),
            created_at=now,
        )
    )
    db.commit()

    scopes = [s for s in code_row.scope.split() if s in SCOPE_CATALOG]
    return {
        "access_token": raw_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "scope": " ".join(scopes),
        "user": _build_userinfo(user, scopes, db),
    }


@router.get("/oauth/userinfo")
def oauth_userinfo(
    request: Request,
    db: Session = Depends(get_session),
) -> dict:
    auth_header = request.headers.get("authorization") or ""
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
    raw_token = auth_header[7:].strip()
    if not raw_token:
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")

    now = utcnow()
    row = db.exec(
        select(OAuthAccessToken).where(
            OAuthAccessToken.token_hash == hash_session_token(raw_token)
        )
    ).first()
    if row is None or row.revoked_at is not None or now >= row.expires_at:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期")

    user = db.get(User, row.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="用户账号不可用")

    scopes = [s for s in row.scope.split() if s in SCOPE_CATALOG]
    return _build_userinfo(user, scopes, db)


# --------------------------------------------------------------------------- #
# 管理员客户端管理
# --------------------------------------------------------------------------- #


class OAuthClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    redirect_uris: list[str] = Field(min_length=1, max_length=20)
    allowed_scopes: list[str] = Field(default_factory=lambda: ["profile", "campus", "nowcoder"])
    is_public: bool = False
    require_campus: bool = True


class OAuthClientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    redirect_uris: list[str] | None = Field(default=None, min_length=1, max_length=20)
    allowed_scopes: list[str] | None = None
    require_campus: bool | None = None
    is_active: bool | None = None


class OAuthClientOut(BaseModel):
    id: int
    client_id: str
    name: str
    description: str
    is_public: bool
    require_campus: bool
    redirect_uris: list[str]
    allowed_scopes: list[str]
    is_active: bool
    created_at: datetime


class OAuthClientCreatedOut(OAuthClientOut):
    #: 机密客户端创建或轮换密钥时仅此一次明文返回；公开客户端为 None。
    client_secret: str | None = None


def _normalize_scopes(raw: list[str]) -> list[str]:
    clean: list[str] = []
    for item in raw:
        key = item.strip()
        if key not in SCOPE_CATALOG:
            raise HTTPException(status_code=400, detail=f"未知的 scope：{key}")
        if key not in clean:
            clean.append(key)
    if not clean:
        raise HTTPException(status_code=400, detail="至少需要选择一个 scope")
    return clean


def _to_client_out(row: OAuthClient) -> OAuthClientOut:
    assert row.id is not None
    return OAuthClientOut(
        id=row.id,
        client_id=row.client_id,
        name=row.name,
        description=row.description,
        is_public=row.is_public,
        require_campus=row.require_campus,
        redirect_uris=_parse_json_list(row.redirect_uris_json),
        allowed_scopes=_parse_json_list(row.allowed_scopes_json),
        is_active=row.is_active,
        created_at=row.created_at,
    )


@router.get("/admin/oauth/clients", response_model=list[OAuthClientOut])
def admin_list_oauth_clients(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> list[OAuthClientOut]:
    rows = db.exec(select(OAuthClient).order_by(OAuthClient.created_at.desc())).all()  # type: ignore[attr-defined]
    return [_to_client_out(r) for r in rows]


@router.post(
    "/admin/oauth/clients",
    response_model=OAuthClientCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
def admin_create_oauth_client(
    body: OAuthClientCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> OAuthClientCreatedOut:
    uris = [_validate_redirect_uri_entry(u) for u in body.redirect_uris]
    scopes = _normalize_scopes(body.allowed_scopes)
    client_id = f"acw_{secrets.token_hex(12)}"
    raw_secret: str | None = None
    secret_hash: str | None = None
    if not body.is_public:
        raw_secret = f"acws_{new_session_token()}"
        secret_hash = hash_session_token(raw_secret)

    row = OAuthClient(
        client_id=client_id,
        name=body.name.strip(),
        description=body.description.strip(),
        client_secret_hash=secret_hash,
        is_public=body.is_public,
        require_campus=body.require_campus,
        redirect_uris_json=json.dumps(uris, ensure_ascii=False),
        allowed_scopes_json=json.dumps(scopes, ensure_ascii=False),
        is_active=True,
        created_by=admin.id,
        created_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return OAuthClientCreatedOut(**_to_client_out(row).model_dump(), client_secret=raw_secret)


@router.patch("/admin/oauth/clients/{id}", response_model=OAuthClientOut)
def admin_update_oauth_client(
    id: int,
    body: OAuthClientUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> OAuthClientOut:
    row = db.get(OAuthClient, id)
    if row is None:
        raise HTTPException(status_code=404, detail="未找到该 OAuth 应用")
    if body.name is not None:
        row.name = body.name.strip()
    if body.description is not None:
        row.description = body.description.strip()
    if body.redirect_uris is not None:
        uris = [_validate_redirect_uri_entry(u) for u in body.redirect_uris]
        row.redirect_uris_json = json.dumps(uris, ensure_ascii=False)
    if body.allowed_scopes is not None:
        scopes = _normalize_scopes(body.allowed_scopes)
        row.allowed_scopes_json = json.dumps(scopes, ensure_ascii=False)
    if body.require_campus is not None:
        row.require_campus = body.require_campus
    if body.is_active is not None:
        row.is_active = body.is_active
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_client_out(row)


@router.post("/admin/oauth/clients/{id}/rotate-secret", response_model=OAuthClientCreatedOut)
def admin_rotate_oauth_secret(
    id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> OAuthClientCreatedOut:
    row = db.get(OAuthClient, id)
    if row is None:
        raise HTTPException(status_code=404, detail="未找到该 OAuth 应用")
    if row.is_public:
        raise HTTPException(status_code=400, detail="公开客户端没有密钥，无需轮换")
    raw_secret = f"acws_{new_session_token()}"
    row.client_secret_hash = hash_session_token(raw_secret)
    db.add(row)
    db.commit()
    db.refresh(row)
    return OAuthClientCreatedOut(**_to_client_out(row).model_dump(), client_secret=raw_secret)


@router.delete("/admin/oauth/clients/{id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_oauth_client(
    id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_session),
) -> None:
    row = db.get(OAuthClient, id)
    if row is None:
        raise HTTPException(status_code=404, detail="未找到该 OAuth 应用")
    db.exec(delete(OAuthAuthCode).where(OAuthAuthCode.client_id == row.client_id))  # type: ignore[arg-type]
    db.exec(delete(OAuthAccessToken).where(OAuthAccessToken.client_id == row.client_id))  # type: ignore[arg-type]
    db.delete(row)
    db.commit()
