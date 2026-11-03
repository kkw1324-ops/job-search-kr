"""스크립트 공용 도구. 경로는 항상 프로젝트 안의 정해진 폴더로만 만든다."""
import json
import re
import unicodedata
from contextlib import contextmanager
from pathlib import Path

ROOT = Path.cwd()
WORK = ROOT / "work"
APPLICATIONS = ROOT / "applications"

# XML 1.0에서 쓸 수 없는 제어 문자
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z가-힣 _-]")


class UserError(Exception):
    """사용자에게 그대로 보여 줄 오류."""


def sanitize_name(value, default, max_len=80):
    if not isinstance(value, str):
        return default
    value = unicodedata.normalize("NFC", value)
    value = _UNSAFE_NAME.sub("", value)
    value = re.sub(r"\s+", " ", value).strip(" -_")
    return value[:max_len].strip() or default


def clean_text(value):
    return _INVALID_XML.sub("", unicodedata.normalize("NFC", value))


def load_json(path):
    if path.is_symlink() or not path.is_file():
        raise UserError(f"{path.relative_to(ROOT)} 파일이 없습니다.")
    if path.stat().st_size > 2_000_000:
        raise UserError(f"{path.relative_to(ROOT)} 파일이 너무 큽니다.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UserError(f"{path.relative_to(ROOT)} 을 읽지 못했습니다: {exc}")


def output_dir(folder, base=None):
    """<base>/<정리한 폴더 이름>/ 을 만들어 돌려준다. base 기본값은 applications. 심볼릭 링크는 거부한다."""
    base = base or APPLICATIONS
    rel = base.name
    name = sanitize_name(folder, "기타")
    if base.is_symlink():
        raise UserError(f"{rel} 폴더가 심볼릭 링크라서 쓰지 않습니다.")
    base.mkdir(parents=True, exist_ok=True)
    target = base / name
    if target.is_symlink():
        raise UserError(f"{rel}/{name} 이 심볼릭 링크라서 쓰지 않습니다.")
    target.mkdir(exist_ok=True)
    if target.resolve().parent != base.resolve():
        raise UserError(f"출력 폴더가 {rel} 밖을 가리킵니다.")
    return target


def unique_path(directory, stem, ext):
    candidate = directory / f"{stem}{ext}"
    n = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f"{stem}-{n}{ext}"
        n += 1
    return candidate


@contextmanager
def atomic_write(path):
    """임시 파일에 쓰게 하고, 끝까지 성공했을 때만 제 이름으로 옮긴다. 실패하면 깨진 파일을 남기지 않는다."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        yield tmp
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
