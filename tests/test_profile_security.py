import json
import tomllib

import pytest

from dryheave.errors import InputError
from dryheave.profile_security import (
    credential_file,
    credential_in_bytes,
    credential_in_config,
    credential_in_live_config,
    credential_in_prose,
    reject_codex_config_secrets,
    reject_secret_fields,
    secret_location,
    sensitive_path,
)


@pytest.mark.parametrize(
    ("content", "finding"),
    [
        ("token=abc123\n", "field token on line 1"),
        ("# notes\npassword: hunter2\n", "field password on line 2"),
        ("export OPENAI_API_KEY='abc'\n", "field OPENAI_API_KEY on line 1"),
        ("curl -H 'Authorization: Bearer abc123def456'\n", "material on line 1"),
        ("key=sk-proj-abcdefghijklmnop\n", "material on line 1"),
        ("x\ny\nGH=ghp_synthetic0123456789abcdef\n", "material on line 3"),
    ],
)
def test_literal_credentials_in_text_are_located(content, finding):
    assert credential_in_bytes(content.encode()) == finding


@pytest.mark.parametrize(
    "content",
    [
        "token=$X\ntoken=$(gh auth token)\n",
        "deploy --password=$DEPLOY_PASSWORD\n",
        'token = ""\n',
        "password: <your password>\n",
        "Use Bearer authentication for every request.\n",
        "cd ~/wrksp/desk-booking-service && npm run task-runner-build\n",
        "weighs_everything_carefully_and_slowly\n",
    ],
)
def test_references_and_lookalike_text_are_not_credentials(content):
    assert credential_in_bytes(content.encode()) is None


@pytest.mark.parametrize(
    ("content", "finding"),
    [
        ('[a]\napi_key = "abc"\n', "field a.api_key"),
        ("[a]\napi_key = 7\n", "field a.api_key"),
        ('[a]\nheaders = { Authorization = "abc" }\n', "field a.headers.Authorization"),
        ('[a]\nurl = "https://x.invalid/mcp?apiKey=abc"\n', "field a.url?apiKey"),
        ('[a]\nargs = ["Bearer abc123def456"]\n', "material on line 2"),
    ],
)
def test_config_credentials_are_located_by_field(content, finding):
    assert credential_in_config(content.encode(), tomllib.loads(content)) == finding


@pytest.mark.parametrize(
    "content",
    [
        '[a]\napi_key = ""\n',
        "[a]\ntoken = true\n",
        '[a]\nenv_vars = ["GITHUB_TOKEN"]\nenv_key = "PROXY_API_KEY"\n',
        '[a]\nbearer_token_env_var = "GH_TOKEN"\n',
        '[a]\nenv_http_headers = { Authorization = "GH_AUTH" }\n',
        '[a]\nurl = "https://x.invalid/mcp?apiKey=$KEY"\n',
        '[a]\nurl = "http://[broken/mcp?apiKey=abc"\n',
        '[a.env]\nMAX_TOKENS = "4096"\nTOKENIZERS_PARALLELISM = "false"\n',
        '# token = "abc"\n[a]\nname = "x"\n',
    ],
)
def test_config_references_and_lookalike_names_are_not_credentials(content):
    assert credential_in_config(content.encode(), tomllib.loads(content)) is None


def test_recipe_fields_name_the_credential_without_its_value():
    with pytest.raises(InputError) as raised:
        reject_secret_fields({"command": {"env": {"API_KEY": "abc"}}}, location="recipe")
    assert str(raised.value) == (
        "Recognized credential field command.env.API_KEY in recipe; use a runtime reference."
    )


@pytest.mark.parametrize(
    ("path", "sensitive"),
    [
        ("skills/x/.env", True),
        ("skills/x/.env.local", True),
        ("skills/x/auth.json", True),
        ("skills/x/.git/config", True),
        ("skills/x/SKILL.md", False),
        ("prompts/history-notes.md", False),
    ],
)
def test_sensitive_paths_cover_credential_history_and_cache_parts(path, sensitive):
    assert sensitive_path(path) is sensitive


@pytest.mark.parametrize(
    ("content", "finding"),
    [
        ("password: string\nsk-1234567890abcdef\n", None),
        ("Authorization: Bearer $TOKEN\nBearer abc123def456\n", None),
        ("key: sk-proj-" + "a1" * 16 + "\n", "material on line 1"),
        ("\n\nghp_" + "a1" * 18 + "\n", "material on line 3"),
        ("Authorization: Bearer " + "a1" * 10 + "\n", "material on line 1"),
        ("-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n", "material on line 1"),
        ("-----BEGIN PRIVATE KEY-----\n...\n", None),
    ],
)
def test_prose_material_needs_realistic_key_lengths(content, finding):
    assert credential_in_prose(content.encode()) == finding


@pytest.mark.parametrize(
    ("name", "refused"),
    [
        (".env", True),
        (".env.local", True),
        ("server.pem", True),
        ("id_ed25519", True),
        ("credentials.json", True),
        (".credentials-dev.json", True),
        ("auth.json", True),
        ("credentials", True),
        (".git-credentials", True),
        ("application_default_credentials.json", True),
        (".pgpass", True),
        (".npmrc", True),
        (".pypirc", True),
        ("credentials.md", False),
        (".env.example", False),
        ("history.md", False),
        ("state_machine.md", False),
        ("SKILL.md", False),
    ],
)
def test_credential_file_names(name, refused):
    assert credential_file(name) is refused


@pytest.mark.parametrize(
    ("content", "finding"),
    [
        ('[projects."/Users/x/sk-learn-experiments"]\ntrust_level = "trusted"\n', None),
        ('[a]\nexperimental_bearer_token = "abc"\n', "field a.experimental_bearer_token"),
        ('[a.http_headers]\nX-Api-Key = "abc"\nX-Title = "x"\n', "field a.http_headers.X-Api-Key"),
        ('[a.http_headers]\nX-Title = "x"\n', None),
        ('# sk-proj-abcdefghijklmnop\n[a]\nname = "x"\n', "material on line 1"),
        ('[a]\napi_key = "abc"\n', "field a.api_key"),
        (
            '[mcp_servers.slack.env]\nSLACK_BOT_TOKEN = "xoxb-1-2-abc"\n',
            "field mcp_servers.slack.env.SLACK_BOT_TOKEN",
        ),
        ('[mcp_servers.hf.env]\nHF_TOKEN = "hf_abc"\n', "field mcp_servers.hf.env.HF_TOKEN"),
        (
            '[mcp_servers.gh.env]\nGITHUB_TOKEN = "' + "0a" * 16 + '"\n',
            "field mcp_servers.gh.env.GITHUB_TOKEN",
        ),
        (
            '[shell_environment_policy.set]\nSESSION_SECRET = "abc"\n',
            "field shell_environment_policy.set.SESSION_SECRET",
        ),
        (
            '[shell_environment_policy.set]\nDATABASE_URL = "postgres://u:hunter2@db/x"\n',
            "URL password in field shell_environment_policy.set.DATABASE_URL",
        ),
        (
            '[mcp_servers.x]\nurl = "https://user:s3cret@host.invalid/mcp"\n',
            "URL password in field mcp_servers.x.url",
        ),
        (
            '[mcp_servers.x]\nargs = ["--db", "mysql://root:pw@localhost/app"]\n',
            "URL password in field mcp_servers.x.args",
        ),
        ('[mcp_servers.gh.env]\nGITHUB_TOKEN = "$GITHUB_TOKEN"\n', None),
        ('[mcp_servers.x.env]\nTOKEN_FILE = "/run/token"\nMAX_TOKENS = "4096"\n', None),
        ('[shell_environment_policy.set]\nTOKENIZERS_PARALLELISM = "false"\n', None),
        ('[mcp_servers.x]\nurl = "https://user@host.invalid/mcp"\n', None),
        ('[mcp_servers.x]\nurl = "https://user:$PASSWORD@host.invalid/mcp"\n', None),
        ('[mcp_servers.x]\nurl = "http://[broken/mcp"\n', None),
        ('[mcp_servers.x]\ntoken_name = "SLACK_BOT_TOKEN"\n', None),
    ],
)
def test_live_config_scans_values_and_codex_token_fields(content, finding):
    assert credential_in_live_config(content.encode(), tomllib.loads(content)) == finding


def test_selected_configs_keep_scanning_table_names():
    content = '[projects."/Users/x/sk-learn-experiments"]\ntrust_level = "trusted"\n'
    assert credential_in_config(content.encode(), tomllib.loads(content)) == "material on line 1"


def test_live_config_scans_hook_environment_tables():
    value = {"hooks": {"Stop": [{"command": "x", "env": {"SLACK_TOKEN": "abc"}}]}}
    assert credential_in_live_config(json.dumps(value).encode(), value) == (
        "field hooks.Stop.env.SLACK_TOKEN"
    )


def test_codex_config_secrets_name_the_field_without_its_value():
    with pytest.raises(InputError) as raised:
        reject_codex_config_secrets(
            {"mcp_servers": {"hf": {"env": {"HF_TOKEN": "hf_synthetic"}}}}, location="asset"
        )
    assert str(raised.value) == (
        "Recognized credential field mcp_servers.hf.env.HF_TOKEN in asset; use a runtime reference."
    )


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        ("/Users/x/.aws/credentials", True),
        ("/Users/x/.AWS", True),
        ("/Users/x/.ssh/id_ed25519.pub", True),
        ("/Users/x/.gnupg/pubring.kbx", True),
        ("/Users/x/.config/gcloud/application_default_credentials.json", True),
        ("/Users/x/.docker/config.json", True),
        ("/Users/x/.kube/config", True),
        ("/Users/x/.config/nvim/init.lua", False),
        ("/Users/x/wrksp/skills/aws/SKILL.md", False),
        ("/Users/x/gcloud/notes.md", False),
    ],
)
def test_secret_locations_name_known_credential_directories(path, secret):
    assert secret_location(path) is secret
