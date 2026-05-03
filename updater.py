from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


BASE_URL = "https://magiaparamor.com/aplicacoes/editor-premiere-premium"
VERSION_URL = f"{BASE_URL}/versao.json"
LISTING_URL = f"{BASE_URL}/"
EXE_NAME = "Editor.exe"
CREDENTIALS_FILE = Path.home() / ".credenciais_rk.json"
USER_AGENT = "EditorPremiere-Updater/3.0"
TIMEOUT_SECONDS = 30
DOWNLOAD_CHUNK_SIZE = 64 * 1024
PROGRESS_BAR_WIDTH = 30


@dataclass
class PackageInfo:
    version: str
    url: str
    filename: str
    source: str


class ZipLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def log(message: str = "") -> None:
    print(message, flush=True)


def fail(message: str, exit_code: int = 1) -> None:
    log(f"[ERRO] {message}")
    raise SystemExit(exit_code)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_windows() -> None:
    if os.name != "nt":
        fail("Este atualizador foi feito para Windows.")


def ensure_write_access(target_dir: Path) -> None:
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        test_file = target_dir / ".__write_test__.tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
    except OSError as exc:
        fail(f"Sem permissao de escrita em '{target_dir}': {exc}")


def request(url: str) -> urllib.request.addinfourl:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS)


def fetch_text(url: str) -> str:
    try:
        with request(url) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Falha HTTP {exc.code} ao acessar {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Falha de conexao ao acessar {url}: {exc.reason}") from exc


def fetch_bytes(url: str) -> bytes:
    try:
        with request(url) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Falha HTTP {exc.code} ao baixar {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Falha de conexao ao baixar {url}: {exc.reason}") from exc


def _format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:6.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:6.2f} GB"


def _render_progress(downloaded: int, total: int) -> None:
    if total > 0:
        ratio = min(downloaded / total, 1.0)
        filled = int(PROGRESS_BAR_WIDTH * ratio)
        bar = "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)
        line = (
            f"      [{bar}] {ratio * 100:6.2f}% "
            f"({_format_size(downloaded)} / {_format_size(total)})"
        )
    else:
        spinner = "|/-\\"[(downloaded // DOWNLOAD_CHUNK_SIZE) % 4]
        line = f"      [{spinner}] Baixado: {_format_size(downloaded)} (tamanho desconhecido)"
    sys.stdout.write("\r" + line)
    sys.stdout.flush()


def download_to_file(url: str, target: Path) -> None:
    try:
        with request(url) as response:
            total_header = response.headers.get("Content-Length")
            try:
                total = int(total_header) if total_header else 0
            except ValueError:
                total = 0

            downloaded = 0
            _render_progress(downloaded, total)
            with target.open("wb") as fh:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    _render_progress(downloaded, total)
            sys.stdout.write("\n")
            sys.stdout.flush()
    except urllib.error.HTTPError as exc:
        sys.stdout.write("\n")
        raise RuntimeError(f"Falha HTTP {exc.code} ao baixar {url}") from exc
    except urllib.error.URLError as exc:
        sys.stdout.write("\n")
        raise RuntimeError(f"Falha de conexao ao baixar {url}: {exc.reason}") from exc


def read_credentials() -> dict[str, str] | None:
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    user_id = str(data.get("user_id", "")).strip()
    chave = str(data.get("chave", "")).strip()
    if not user_id or not chave:
        return None
    return {"user_id": user_id, "chave": chave}


def ask_credentials() -> dict[str, str]:
    log("[1/5] Verificando credenciais...")
    creds = read_credentials()
    if creds:
        log("      Credenciais encontradas.")
        return creds

    log("      Nenhuma credencial valida foi encontrada.")
    user_id = input("      Usuario: ").strip()
    chave = input("      Chave: ").strip()
    if not user_id:
        fail("Usuario nao informado.")
    if not chave:
        fail("Chave nao informada.")

    creds = {"user_id": user_id, "chave": chave}
    try:
        CREDENTIALS_FILE.write_text(
            json.dumps(creds, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        fail(f"Falha ao salvar as credenciais em '{CREDENTIALS_FILE}': {exc}")

    log(f"      Credenciais salvas em '{CREDENTIALS_FILE}'.")
    return creds


def normalize_url(candidate: str, base_url: str = LISTING_URL) -> str:
    return urllib.parse.urljoin(base_url, candidate)


def try_manifest() -> PackageInfo | None:
    try:
        content = fetch_text(VERSION_URL)
        data = json.loads(content)
    except Exception:
        return None

    url = str(data.get("url", "")).strip()
    if not url:
        return None

    absolute_url = normalize_url(url)
    filename = str(data.get("arquivo", "")).strip() or Path(
        urllib.parse.urlparse(absolute_url).path
    ).name
    version = str(data.get("versao", "")).strip() or f"detected-{filename}"
    return PackageInfo(version=version, url=absolute_url, filename=filename, source="manifesto")


def parse_version_from_name(filename: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+(?:[._-]\d+)+)", filename)
    if not match:
        return None
    try:
        return tuple(int(part) for part in re.split(r"[._-]", match.group(1)))
    except ValueError:
        return None


def discover_from_listing() -> PackageInfo:
    content = fetch_text(LISTING_URL)
    parser = ZipLinkParser()
    parser.feed(content)

    regex_links = re.findall(
        r"""href\s*=\s*['"]([^'"]+\.zip(?:\?[^'"]*)?)['"]""",
        content,
        flags=re.IGNORECASE,
    )
    candidates: list[PackageInfo] = []

    for href in dict.fromkeys(parser.links + regex_links):
        absolute_url = normalize_url(href)
        filename = Path(urllib.parse.urlparse(absolute_url).path).name
        if not filename.lower().endswith(".zip"):
            continue
        version_tuple = parse_version_from_name(filename)
        version_text = (
            ".".join(str(x) for x in version_tuple) if version_tuple else f"detected-{filename}"
        )
        candidates.append(
            PackageInfo(
                version=version_text,
                url=absolute_url,
                filename=filename,
                source="listagem",
            )
        )

    if not candidates:
        fail("Nenhum arquivo ZIP foi encontrado no servidor.")

    candidates.sort(
        key=lambda item: (
            parse_version_from_name(item.filename) is not None,
            parse_version_from_name(item.filename) or (),
            item.filename.lower(),
        ),
        reverse=True,
    )
    return candidates[0]


def discover_package() -> PackageInfo:
    log("[2/5] Procurando a versao mais recente no servidor...")
    package = try_manifest()
    if package is None:
        package = discover_from_listing()
    log(f"      Pacote encontrado via {package.source}: {package.filename}")
    log(f"      Versao identificada: {package.version}")
    return package


def download_package(package: PackageInfo, temp_dir: Path) -> Path:
    log("[3/5] Baixando o pacote...")
    log("      Isso pode demorar alguns minutos dependendo da sua conexao. Aguarde, o programa nao travou.")
    target = temp_dir / package.filename
    try:
        download_to_file(package.url, target)
    except RuntimeError as exc:
        fail(str(exc))
    except OSError as exc:
        fail(f"Nao foi possivel salvar o pacote em '{target}': {exc}")

    if not target.exists() or target.stat().st_size == 0:
        fail("O pacote ZIP foi baixado vazio ou nao foi salvo.")

    try:
        with zipfile.ZipFile(target, "r") as zf:
            bad_entry = zf.testzip()
            if bad_entry:
                fail(f"O ZIP baixado esta corrompido. Primeiro arquivo com erro: {bad_entry}")
    except zipfile.BadZipFile:
        fail("O arquivo baixado nao e um ZIP valido.")

    return target


def terminate_running_app(target_dir: Path) -> None:
    exe_path = target_dir / EXE_NAME
    if not exe_path.exists():
        return

    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if EXE_NAME.lower() not in result.stdout.lower():
        return

    log("      O programa esta aberto. Tentando encerrar para atualizar...")
    subprocess.run(["taskkill", "/IM", EXE_NAME, "/T", "/F"], check=False)
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {EXE_NAME}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if EXE_NAME.lower() in result.stdout.lower():
        fail(f"Nao foi possivel fechar '{EXE_NAME}'. Feche o programa manualmente e execute novamente.")


def find_payload_root(extract_dir: Path) -> Path:
    entries = [entry for entry in extract_dir.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_dir


def install_package(zip_path: Path, target_dir: Path) -> None:
    log(f"[4/5] Instalando em '{target_dir}'...")
    extract_dir = zip_path.parent / "extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except (OSError, zipfile.BadZipFile) as exc:
        fail(f"Falha ao extrair o ZIP: {exc}")

    payload_root = find_payload_root(extract_dir)
    terminate_running_app(target_dir)

    for source in payload_root.rglob("*"):
        relative = source.relative_to(payload_root)
        destination = target_dir / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def launch_program(target_dir: Path, version: str) -> None:
    log("[5/5] Finalizando...")
    log("")
    log("============================================")
    log(f"  Instalacao concluida! Versao {version}")
    log("============================================")
    log("")

    exe_path = target_dir / EXE_NAME
    if not exe_path.exists():
        log(f"[AVISO] {EXE_NAME} nao foi encontrado apos a instalacao.")
        return

    log(f"Abrindo {EXE_NAME}...")
    subprocess.Popen([str(exe_path)], cwd=str(target_dir))


def main() -> int:
    ensure_windows()
    target_dir = app_dir()
    ensure_write_access(target_dir)

    log("")
    log("============================================")
    log("  Automatizador do Premiere - Instalador")
    log("============================================")
    log("")

    ask_credentials()

    with tempfile.TemporaryDirectory(prefix="EditorPremiereUpdater_") as tmp:
        temp_dir = Path(tmp)
        package = discover_package()
        zip_path = download_package(package, temp_dir)
        install_package(zip_path, target_dir)
        launch_program(target_dir, package.version)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        fail("Operacao cancelada pelo usuario.")
