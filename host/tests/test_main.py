"""HTTP-level tests for main.py, focused on the shell-injection fix on
/agent.sh and /connect's model/token query params.

Regression coverage for a real vulnerability: model/token used to be
substituted into agent_template.sh (and the /connect one-liner) with zero
validation. Both land inside double-quoted bash assignments, and bash still
runs $(...) command substitution inside double quotes -- so a crafted model
value became arbitrary code execution on whoever ran the resulting
one-liner. Confirmed exploitable with a real payload (a file got created)
before _validate_model/_validate_token went in.

Run with:   cd host && uv run pytest -v -k main
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_agent_sh_rejects_command_substitution_in_model():
    resp = client.get("/agent.sh", params={"token": "x", "model": "$(touch /tmp/pwned)"})
    assert resp.status_code == 400


def test_agent_sh_rejects_command_substitution_in_token():
    resp = client.get("/agent.sh", params={"token": "$(whoami)", "model": "hermes3"})
    assert resp.status_code == 400


def test_agent_sh_rejects_double_quote_breakout():
    resp = client.get("/agent.sh", params={"token": "x", "model": '"; touch /tmp/pwned; echo "'})
    assert resp.status_code == 400


def test_agent_sh_accepts_real_model_names():
    for model in ["hermes3", "qwen2.5:7b", "llama3.1:8b-instruct-q4_0", "library/model"]:
        resp = client.get("/agent.sh", params={"token": "x", "model": model})
        assert resp.status_code == 200, model
        assert f'MODEL="{model}"' in resp.text


def test_connect_rejects_command_substitution_in_model():
    resp = client.get("/connect", params={"model": "$(touch /tmp/pwned)"})
    assert resp.status_code == 400


def test_connect_accepts_real_model_names():
    resp = client.get("/connect", params={"model": "hermes3"})
    assert resp.status_code == 200


# /agent.ps1 embeds the same values into a PowerShell script instead of
# bash. PowerShell double-quoted strings ALSO interpolate $(...) and
# $variable, so the same class of injection applies -- this reuses
# _validate_model/_validate_token, but it's worth its own coverage in case
# that reuse is ever "simplified" away.
def test_agent_ps1_rejects_command_substitution_in_model():
    resp = client.get("/agent.ps1", params={"token": "x", "model": "$(Remove-Item -Recurse C:\\)"})
    assert resp.status_code == 400


def test_agent_ps1_rejects_command_substitution_in_token():
    resp = client.get("/agent.ps1", params={"token": "$(whoami)", "model": "hermes3"})
    assert resp.status_code == 400


def test_agent_ps1_accepts_real_model_names():
    for model in ["hermes3", "qwen3:8b", "llama3.1:8b-instruct-q4_0"]:
        resp = client.get("/agent.ps1", params={"token": "x", "model": model})
        assert resp.status_code == 200, model
        assert f'$Model   = "{model}"' in resp.text
