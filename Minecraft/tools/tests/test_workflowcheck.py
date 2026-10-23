import mcdeploy.workflowcheck as wc

WF = ".github/workflows/minecraft.yml"
GOOD = """
jobs:
  validate:
    runs-on: ubuntu-latest
    env:
      GITHUB_TOKEN: ${{ github.token }}
  backend:
    env:
      MC_DB_PASSWORD: ${{ secrets.MC_DB_PASSWORD }}
      MC_FLAG: ${{ vars.MC_FLAG }}
      OTHER: plain
    steps:
      - run: echo hi
"""


def run(tmp_path, monkeypatch, text, want=None):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / WF).write_text(text)
    monkeypatch.setattr(wc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(wc, "expected_jobs", lambda: {(WF, "backend"): want or {"MC_DB_PASSWORD": "secrets", "MC_FLAG": "vars"}})
    return wc.check_workflows()


def test_exact_list_passes(tmp_path, monkeypatch):
    assert run(tmp_path, monkeypatch, GOOD) == []


def test_secret_dump_is_rejected(tmp_path, monkeypatch):
    problems = run(tmp_path, monkeypatch, GOOD.replace("OTHER: plain", "ALL: ${{ toJSON(secrets) }}"))
    assert any("hands out every secret" in p for p in problems)


def test_missing_extra_and_renamed_entries(tmp_path, monkeypatch):
    text = GOOD.replace("MC_FLAG: ${{ vars.MC_FLAG }}", "EXTRA: ${{ secrets.EXTRA }}\n      PW2: ${{ secrets.MC_DB_PASSWORD }}")
    problems = run(tmp_path, monkeypatch, text)
    assert any("missing env MC_FLAG" in p for p in problems)
    assert any("receives EXTRA but does not need it" in p for p in problems)
    assert any("env PW2 must be" in p for p in problems)


def test_secret_outside_job_env_is_rejected(tmp_path, monkeypatch):
    text = GOOD.replace("- run: echo hi", "- run: echo ${{ secrets.MC_DB_PASSWORD }}")
    assert any("must be passed via the job's env" in p for p in run(tmp_path, monkeypatch, text))


def test_other_jobs_get_no_secrets(tmp_path, monkeypatch):
    text = GOOD.replace("GITHUB_TOKEN: ${{ github.token }}", "LEAK: ${{ secrets.LEAK }}")
    assert any("validate: must not receive secrets" in p for p in run(tmp_path, monkeypatch, text))
