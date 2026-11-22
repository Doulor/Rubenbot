

def test_session_survives_restart_with_persisted_secret(tmp_path):
    """落盘密钥后，新进程的 signer 必须认旧进程签发的 token（重启不登出）。"""
    from rubenbot.web.auth import SessionSigner, load_or_create_session_secret

    db_path = str(tmp_path / "rubenbot.db")
    secret1 = load_or_create_session_secret(db_path)
    assert secret1 is not None and len(secret1) >= 16
    first = SessionSigner("pw", ttl_hours=720, secret=secret1)
    token = first.issue("admin")

    secret2 = load_or_create_session_secret(db_path)
    assert secret2 == secret1  # 第二次是读回，不是重新生成
    second = SessionSigner("pw", ttl_hours=720, secret=secret2)
    session = second.parse(token)
    assert session is not None and session.user == "admin"
