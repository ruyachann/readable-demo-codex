import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

JSR = ROOT / "english_paper" / "JSR-34-e70000.pdf"
MDPI = ROOT / "english_paper" / "jzbg-07-00024.pdf"


@pytest.fixture(scope="session")
def jsr():
    if not JSR.exists():
        pytest.skip("sample PDF missing")
    from readable.extract import extract_pdf
    return extract_pdf(JSR)


@pytest.fixture(scope="session")
def mdpi():
    if not MDPI.exists():
        pytest.skip("sample PDF missing")
    from readable.extract import extract_pdf
    return extract_pdf(MDPI)


def frames_of(doc):
    return {f["id"]: f for p in doc["pages"] for f in p["frames"]}


@pytest.fixture(autouse=True)
def _clean_claude_api_env(monkeypatch):
    """テストでは API キー・ゲートウェイ設定を親の環境から外す (開発セッションは ANTHROPIC_BASE_URL を持つ)。個別のテストが必要なら setenv する。"""
    from readable.structure import API_ENV_VARS
    for k in API_ENV_VARS:
        monkeypatch.delenv(k, raising=False)
