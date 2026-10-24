"""Envia ao grupo do Spotline News, pela Evolution API, o que a nuvem põe na fila.

Roda no PC (Docker ou Python 3.10+, só biblioteca padrão). A cada 2 minutos lê
spotline/AAAA-MM-DD/fila.json da branch ms-midia (repositório público lexai-31-03-2026) e manda
cada item que ainda não mandou: o PDF da manhã (documento) e as postagens dos alertas (imagem
com o texto de legenda). O que já foi enviado fica anotado em ESTADO (enviados.json), então
reiniciar não manda nada repetido.

Variáveis de ambiente:
  EVOLUTION_API_URL    ex.: http://evolution-api:8080 (a mesma do envio do escritório)
  EVOLUTION_API_KEY    a chave da Evolution (nunca no código nem no chat)
  EVOLUTION_INSTANCE   o nome da instância conectada ao WhatsApp
  SPOTLINE_GRUPO       o id do grupo (termina em @g.us); veja com: python enviar.py --grupos
  ESTADO               arquivo dos enviados (padrão: enviados.json ao lado deste programa)

Uso: python enviar.py            fica rodando (Ctrl+C para parar)
     python enviar.py --uma-vez   uma rodada só
     python enviar.py --grupos    lista os grupos da instância (para achar o SPOTLINE_GRUPO)
     python enviar.py --conectar  cria a instância EVOLUTION_INSTANCE (se ainda não existe) e
                                  salva o QR Code em qrcode.png: escaneie com o WhatsApp do
                                  número do Spotline (Aparelhos conectados > Conectar aparelho)
     python enviar.py --codigo 551153220181
                                  em vez do QR, um código de 8 letras para digitar no celular
                                  (Conectar aparelho > Conectar com número de telefone)
     python enviar.py --sair      desconecta a sessão da instância (para conectar do zero)
     python enviar.py --recriar 551153220181
                                  apaga e cria de novo a instância já com o número, e mostra o
                                  código de pareamento (e salva o QR em qrcode.png)
"""
import base64
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAW = os.environ.get("SPOTLINE_RAW",
                     "https://raw.githubusercontent.com/rfsbastter-a11y/lexai-31-03-2026/ms-midia/spotline").rstrip("/")
BRT = timezone(timedelta(hours=-3))
INTERVAL = 120
MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def log(msg: str) -> None:
    print(f"[{datetime.now(BRT):%d/%m %H:%M:%S}] {msg}", flush=True)


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Falta a variável de ambiente {name} (veja o começo deste arquivo).")
    return value


def get(url: str) -> bytes | None:
    """Arquivo da ms-midia; None se ainda não existe (404)."""
    req = urllib.request.Request(url, headers={"User-Agent": "spotline-enviar", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def evolution(path: str, body: dict | None = None, method: str | None = None) -> dict:
    url = env("EVOLUTION_API_URL").rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if body is not None else "GET"),
                                 headers={"apikey": env("EVOLUTION_API_KEY"), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def send(item: dict, content: bytes) -> None:
    """Uma mensagem com o arquivo e a legenda. Tenta o formato da Evolution v2 e, se a
    instalação for a v1, o formato antigo."""
    instance = urllib.parse.quote(env("EVOLUTION_INSTANCE"))
    ext = Path(item["arquivo"]).suffix.lower()
    media = {"mediatype": "document" if item["tipo"] == "documento" else "image",
             "mimetype": MIME.get(ext, "application/octet-stream"), "caption": item["legenda"],
             "media": base64.b64encode(content).decode(), "fileName": item["arquivo"]}
    number = env("SPOTLINE_GRUPO")
    try:
        evolution(f"/message/sendMedia/{instance}", {"number": number, **media})
    except urllib.error.HTTPError as e:
        if e.code not in (400, 404):
            raise
        evolution(f"/message/sendMedia/{instance}", {"number": number, "mediaMessage": media})


def load_state(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return set()


def run_once(state_path: Path) -> None:
    day = datetime.now(BRT).strftime("%Y-%m-%d")
    raw = get(f"{RAW}/{day}/fila.json?t={int(time.time())}")
    if not raw:
        return
    queue = json.loads(raw)
    if not state_path.exists():
        # Primeira vez: o que já está na fila de hoje fica como enviado (senão o grupo receberia
        # de uma vez todas as postagens do dia). Daqui em diante, só o que entrar de novo.
        state_path.write_text(json.dumps(sorted(f"{day}/{q['id']}" for q in queue), ensure_ascii=False, indent=1),
                              encoding="utf-8")
        log(f"primeira rodada: {len(queue)} item(ns) de hoje marcados como já enviados")
        return
    for item in queue:
        key = f"{day}/{item['id']}"
        sent = load_state(state_path)  # relido a cada item: o que outro envio já mandou não sai de novo
        if key in sent:
            continue
        content = get(f"{RAW}/{day}/{urllib.parse.quote(item['arquivo'])}")
        if content is None:
            log(f"{item['arquivo']}: ainda não chegou ao GitHub; tento na próxima rodada")
            return  # mantém a ordem: não pula para o item seguinte
        send(item, content)
        sent.add(key)
        state_path.write_text(json.dumps(sorted(sent), ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"enviado: {item['arquivo']}")
        time.sleep(3)


def connect() -> None:
    """Cria a instância do número do Spotline (separada da do escritório) e mostra o QR Code."""
    name = env("EVOLUTION_INSTANCE")
    instance = urllib.parse.quote(name)
    try:
        state = evolution(f"/instance/connectionState/{instance}").get("instance", {}).get("state")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        state = None
    if state == "open":
        print(f"A instância {name} já está conectada ao WhatsApp.")
        return
    if state is None:
        evolution("/instance/create", {"instanceName": name, "qrcode": True, "integration": "WHATSAPP-BAILEYS"})
        print(f"Instância {name} criada.")
    qr = evolution(f"/instance/connect/{instance}")
    b64 = (qr.get("base64") or "").split(",")[-1]
    if not b64:
        print(f"A Evolution não devolveu o QR Code: {str(qr)[:200]}")
        return
    out = Path(os.environ.get("QRCODE") or Path(__file__).with_name("qrcode.png"))
    out.write_bytes(base64.b64decode(b64))
    print(f"QR Code salvo em {out}. No celular do número do Spotline: WhatsApp > Aparelhos conectados >"
          " Conectar aparelho, e escaneie. Depois rode de novo --conectar para conferir.")


def pairing_code(number: str) -> None:
    """Código de pareamento: alternativa ao QR quando o WhatsApp não aceita escanear."""
    instance = urllib.parse.quote(env("EVOLUTION_INSTANCE"))
    reply = evolution(f"/instance/connect/{instance}?number={urllib.parse.quote(number)}")
    code = reply.get("pairingCode")
    if not code:
        print(f"A Evolution não devolveu o código: {str(reply)[:200]}")
        return
    print(f"Código: {code}\nNo celular do Spotline: WhatsApp > Aparelhos conectados > Conectar aparelho >"
          " Conectar com número de telefone, e digite o código (vale poucos minutos).")


def recreate(number: str) -> None:
    """Apaga a instância e cria de novo com o número: a Evolution só devolve o código de
    pareamento quando a instância nasce com ele."""
    name = env("EVOLUTION_INSTANCE")
    instance = urllib.parse.quote(name)
    for path in (f"/instance/logout/{instance}", f"/instance/delete/{instance}"):
        try:
            evolution(path, method="DELETE")
        except urllib.error.HTTPError as e:
            print(f"{path}: a Evolution respondeu {e.code} (segue)")
    time.sleep(3)
    reply = evolution("/instance/create", {"instanceName": name, "qrcode": True, "number": number,
                                           "integration": "WHATSAPP-BAILEYS"})
    qr = reply.get("qrcode") or {}
    code = qr.get("pairingCode") or reply.get("pairingCode")
    b64 = (qr.get("base64") or "").split(",")[-1]
    if b64:
        out = Path(os.environ.get("QRCODE") or Path(__file__).with_name("qrcode.png"))
        out.write_bytes(base64.b64decode(b64))
        print(f"QR Code salvo em {out}.")
    if code:
        print(f"Código de pareamento: {code}\nNo celular do Spotline: WhatsApp > Aparelhos conectados >"
              " Conectar aparelho > Conectar com número de telefone, e digite o código.")
    else:
        print("A Evolution não devolveu código de pareamento; use o QR Code. Se o celular recusar,"
              " a Evolution precisa ser atualizada.")
    try:
        version = evolution("/").get("version")
        print(f"Versão da Evolution: {version}")
    except Exception:
        pass


def logout() -> None:
    instance = urllib.parse.quote(env("EVOLUTION_INSTANCE"))
    try:
        evolution(f"/instance/logout/{instance}", method="DELETE")
        print("Sessão desconectada. Agora rode --codigo ou --conectar.")
    except urllib.error.HTTPError as e:
        print(f"A Evolution respondeu {e.code} (talvez já estivesse desconectada). Siga com --codigo ou --conectar.")


def list_groups() -> None:
    instance = urllib.parse.quote(env("EVOLUTION_INSTANCE"))
    groups = evolution(f"/group/fetchAllGroups/{instance}?getParticipants=false")
    for g in groups if isinstance(groups, list) else []:
        print(f"{g.get('id')}  {g.get('subject')}")


def main() -> int:
    if "--conectar" in sys.argv[1:]:
        connect()
        return 0
    if "--sair" in sys.argv[1:]:
        logout()
        return 0
    if "--recriar" in sys.argv[1:]:
        args = sys.argv[1:]
        i = args.index("--recriar")
        if i + 1 >= len(args):
            sys.exit("Use: python enviar.py --recriar 5511XXXXXXXX (DDI + DDD + número, só dígitos)")
        recreate(args[i + 1])
        return 0
    if "--codigo" in sys.argv[1:]:
        args = sys.argv[1:]
        i = args.index("--codigo")
        if i + 1 >= len(args):
            sys.exit("Use: python enviar.py --codigo 5511XXXXXXXX (DDI + DDD + número, só dígitos)")
        pairing_code(args[i + 1])
        return 0
    if "--grupos" in sys.argv[1:]:
        list_groups()
        return 0
    for name in ("EVOLUTION_API_URL", "EVOLUTION_API_KEY", "EVOLUTION_INSTANCE", "SPOTLINE_GRUPO"):
        env(name)
    state_path = Path(os.environ.get("ESTADO") or Path(__file__).with_name("enviados.json"))
    # Um envio só por vez: dois rodando juntos (o do container e um "docker exec" esquecido)
    # mandavam cada notícia duas vezes ao grupo.
    lock = open(state_path.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Já há um envio rodando (o do container). Este não vai mandar nada.")
    log(f"enviando ao grupo {os.environ['SPOTLINE_GRUPO']}; anotações em {state_path}")
    while True:
        try:
            run_once(state_path)
        except Exception as e:  # rede ou Evolution fora do ar: tenta de novo na próxima rodada
            log(f"falha nesta rodada ({type(e).__name__}: {str(e)[:200]}); tento de novo em {INTERVAL}s")
        if "--uma-vez" in sys.argv[1:]:
            return 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
