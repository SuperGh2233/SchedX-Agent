from schedx.llm.client import LLMClient


def test_llm_client_has_no_hardcoded_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SCHEDX_LLM_API_KEY", raising=False)
    monkeypatch.setenv("SCHEDX_LLM_ENV_FILE", str(tmp_path / "missing"))
    assert not LLMClient().is_configured()


def test_llm_client_reads_external_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "llm.env"
    secret.write_text(
        "SCHEDX_LLM_API_KEY=test-key\nSCHEDX_LLM_MODEL=deepseek-v4-pro\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHEDX_LLM_ENV_FILE", str(secret))
    client = LLMClient()
    assert client.api_key == "test-key"
    assert client.model == "deepseek-v4-pro"
