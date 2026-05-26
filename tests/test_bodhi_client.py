from app.bodhi_client import BodhiClient


def test_bodhi_client_strips_wrapping_quotes_from_env_values(monkeypatch):
    monkeypatch.setenv("BODHI_API_BASE_URL", '"https://sapientaiproducts.com/save"')
    monkeypatch.setenv("BODHI_PAT_TOKEN", '"pat_test_token"')

    client = BodhiClient()

    assert client.base_url == "https://sapientaiproducts.com/save"
    assert client.token == "pat_test_token"
    assert client.headers()["Authorization"] == "Bearer pat_test_token"


def test_bodhi_client_strips_whitespace_from_env_values(monkeypatch):
    monkeypatch.setenv("BODHI_API_BASE_URL", "  https://sapientaiproducts.com/save/  ")
    monkeypatch.setenv("BODHI_PAT_TOKEN", "  pat_test_token\n")

    client = BodhiClient()

    assert client.base_url == "https://sapientaiproducts.com/save"
    assert client.token == "pat_test_token"
    assert client.headers()["Authorization"] == "Bearer pat_test_token"
