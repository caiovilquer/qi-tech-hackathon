"""Simulação da política v2 do motor antifraude Pix — sem banco, sem HTTP.

Serve de oráculo para a tabela de calibração (seção 4.3 da avaliação v2):
cada cenário monta um histórico em memória, manda os Pix em ordem e
imprime status, score por família e regras disparadas. Os nomes das regras
são os códigos que devem sair em `metadata.reasons[].code`.

Cobre as regras das ondas 1 e 2, mais RECENT_ACCOUNT_RECOVERY. Ficam de
fora PASS_THROUGH, SHARED_DEVICE e PAYER_FLAGGED (onda 3).

Simplificações que a implementação real não deve copiar:
  * o resultado do 2FA (webhook) é aplicado no próprio envio; na API ele
    chega depois, com `confirmed_at`, e só vale para Pix posteriores;
  * o histórico é uma lista em memória; na API, são as consultas do
    repository (histórico de 90 dias do pagador e entrada do destino).

Uso:  python3 politica_v2_simulacao.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
R = 100  # centavos por real

APPROVED, TWO_FA, REJECTED = 0, 1, 2
STATUS = {APPROVED: "approved", TWO_FA: "two_factor_required", REJECTED: "rejected"}

# ── Política v2 ──────────────────────────────────────────────────────────
NIGHT_SOFT = 1_000 * R
NIGHT_HARD = 3_000 * R
LOW_HISTORY = (1_000 * R, 5_000 * R)
PJ_FACTOR = 5
STRUCTURING_LIMITS = (1_000 * R, 3_000 * R, 5_000 * R)
CAPS = {"Limite": 30, "Velocity": 30, "Montante": 30, "Perfil": 30, "Acesso": 40, "Contraparte": 50}
TWO_FA_AT, REJECT_AT = 30, 70


@dataclass
class Pix:
    id: str
    payer: str
    payee: str
    amount: int
    t: datetime
    dest_account: str
    dest_opened: datetime
    device: str = "dev-1"
    status: Optional[int] = None
    tfa: Optional[str] = None  # resultado do 2FA vindo do webhook: passed | failed

    @property
    def payer_pf(self) -> bool:
        return len(self.payer) == 11

    @property
    def payee_pf(self) -> bool:
        return len(self.payee) == 11


@dataclass
class Mocks:
    authorized_devices: dict = field(default_factory=dict)   # doc -> {device}
    recoveries: dict = field(default_factory=dict)           # doc -> [datetime]
    dict_markers: dict = field(default_factory=dict)          # doc -> [(datetime, tipo, notificador)]
    dict_open_reports: dict = field(default_factory=dict)     # doc -> [(datetime, notificador)]
    dict_down: bool = False


def effective(p: Pix) -> bool:
    """Pagamento provavelmente efetivado: aprovado, ou 2FA confirmado pelo webhook."""
    return p.status == APPROVED or (p.status == TWO_FA and p.tfa == "passed")


def exposure(p: Pix) -> bool:
    """Dinheiro que pode ter saído: aprovado ou 2FA não recusado."""
    return p.status == APPROVED or (p.status == TWO_FA and p.tfa != "failed")


def local(t: datetime) -> datetime:
    return t.astimezone(TZ)


def night_start(t: datetime) -> Optional[datetime]:
    lt = local(t)
    if lt.hour >= 20:
        return lt.replace(hour=20, minute=0, second=0, microsecond=0)
    if lt.hour < 6:
        return (lt - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    return None


def dict_stats(doc: str, t: datetime, m: Mocks) -> dict:
    """Mock do DICT "na data da transação": só enxerga marcações já criadas."""
    if m.dict_down:
        return {"state": "unavailable"}
    marks = [x for x in m.dict_markers.get(doc, []) if x[0] <= t]
    d90 = [x for x in marks if x[0] > t - timedelta(days=90)]
    m12 = [x for x in marks if x[0] > t - timedelta(days=365)]
    opened = [x for x in m.dict_open_reports.get(doc, []) if x[0] <= t]
    return {
        "state": "ok",  # documento desconhecido = tudo zerado, como o DICT real faria
        "mule_scammer_d90": len([x for x in d90 if x[1] in ("MULE", "SCAMMER")]),
        "reporters_d90": len({x[2] for x in d90}),
        "markers_d90": len(d90),
        "markers_m12": len(m12),
        "open_reports": len(opened),
    }


def evaluate(p: Pix, hist: list[Pix], m: Mocks) -> tuple[int, int, dict, list[str]]:
    hits: list[tuple[str, str, int]] = []  # (família, código, pontos)
    floors: list[tuple[int, str]] = []
    t = p.t
    same_holder = p.payer == p.payee
    dest_age = t - p.dest_opened
    hist = [h for h in hist if h.t <= t]  # nada do futuro entra na janela
    mine = [h for h in hist if h.payer == p.payer]

    # Perfil: pagamentos efetivados em dias anteriores ao atual
    prev_days = [h for h in mine if effective(h) and h.t <= t - timedelta(hours=24)]
    has_profile = len(prev_days) >= 5 and len({local(h.t).date() for h in prev_days}) >= 3

    paid_payee_before = [h for h in mine if h.payee == p.payee and effective(h)]
    established = any(h.t <= t - timedelta(hours=24) for h in paid_payee_before)
    is_new_payee = not paid_payee_before and not same_holder

    # ── Limite ──
    ns = night_start(t)
    if ns is not None and p.payer_pf and p.payee_pf and not same_holder:
        night_pf = [h for h in mine if exposure(h) and h.payee_pf and h.payee != h.payer and h.t >= ns]
        if sum(h.amount for h in night_pf) + p.amount > NIGHT_SOFT:
            floors.append((TWO_FA, "NIGHT_SOFT_LIMIT"))
    if ns is not None and p.payer_pf:
        def qualifies(x: Pix, at: datetime, hist_x: list[Pix]) -> bool:
            if x.payer == x.payee:
                return (at - x.dest_opened) < timedelta(days=7)
            if not x.payee_pf:
                return False
            est = any(h.payee == x.payee and effective(h) and h.t <= at - timedelta(hours=24) for h in hist_x)
            return not est
        if qualifies(p, t, mine):
            night_q = [h for h in mine if exposure(h) and h.t >= ns and qualifies(h, h.t, [x for x in mine if x.t < h.t])]
            if sum(h.amount for h in night_q) + p.amount > NIGHT_HARD:
                floors.append((REJECTED, "NIGHT_HARD_LIMIT"))
    if not has_profile:
        f = 1 if p.payer_pf else PJ_FACTOR
        if p.amount > LOW_HISTORY[1] * f:
            hits.append(("Limite", "LOW_HISTORY_EXPOSURE", 30))
        elif p.amount > LOW_HISTORY[0] * f:
            hits.append(("Limite", "LOW_HISTORY_EXPOSURE", 15))

    # ── Velocity ── (tentativas em qualquer status; retry = mesmo favorecido e valor em 2 min)
    if p.payer_pf:
        window = sorted([h for h in mine if h.t > t - timedelta(minutes=10)] + [p], key=lambda x: x.t)
        dedup: list[Pix] = []
        for x in window:
            if not any(y.payee == x.payee and y.amount == x.amount and x.t - y.t <= timedelta(minutes=2) for y in dedup):
                dedup.append(x)
        if len(dedup) >= 10:
            floors.append((REJECTED, "VELOCITY_BURST"))
        elif len(dedup) >= 5:
            hits.append(("Velocity", "VELOCITY_BURST", 30))
    if has_profile:
        hour = [h for h in mine if h.t > t - timedelta(hours=1)]
        new_payees = set()
        for h in hour:
            before = [x for x in mine if x.t < h.t and x.payee == h.payee and effective(x)]
            if not before and h.payee != h.payer:
                new_payees.add(h.payee)
        if is_new_payee:
            new_payees.add(p.payee)
        if len(new_payees) >= 3:
            hits.append(("Velocity", "NEW_PAYEE_FANOUT", 20))

    # ── Montante ──
    day = [h for h in mine if exposure(h) and h.t > t - timedelta(hours=24)]
    sum24 = sum(h.amount for h in day) + p.amount
    if len(day) + 1 >= 2:
        if not has_profile and sum24 > 5_000 * R * (1 if p.payer_pf else PJ_FACTOR):
            hits.append(("Montante", "DAILY_AMOUNT", 20))
        if has_profile:
            daily: dict = {}
            for h in prev_days:
                daily[local(h.t).date()] = daily.get(local(h.t).date(), 0) + h.amount
            if sum24 > 3 * max(daily.values()):
                hits.append(("Montante", "DAILY_AMOUNT", 20))
    attempts24 = [h for h in mine if h.t > t - timedelta(hours=24)] + [p]
    for lim in STRUCTURING_LIMITS:
        if len([h for h in attempts24 if 0.85 * lim <= h.amount <= lim]) >= 3 and 0.85 * lim <= p.amount <= lim:
            hits.append(("Montante", "STRUCTURING", 30))
            break

    # ── Perfil ──
    if has_profile:
        same = [h for h in prev_days if h.payee == p.payee]
        ref = max(h.amount for h in same) if len(same) >= 2 else max(h.amount for h in prev_days)
        ratio = p.amount / ref
        if ratio > 5:
            hits.append(("Perfil", "AMOUNT_ABOVE_PROFILE", 30))
        elif ratio > 2:
            hits.append(("Perfil", "AMOUNT_ABOVE_PROFILE", 15))

    # ── Contraparte ──
    if dest_age < timedelta(days=7):
        hits.append(("Contraparte", "RECENT_PAYEE_ACCOUNT", 25))
    elif dest_age < timedelta(days=30):
        hits.append(("Contraparte", "RECENT_PAYEE_ACCOUNT", 10))
    if has_profile and is_new_payee:
        hits.append(("Contraparte", "NEW_PAYEE", 10))
    if dest_age < timedelta(days=30):
        inbound = [h for h in hist if h.dest_account == p.dest_account and h.t > t - timedelta(hours=24)]
        payers = {h.payer for h in inbound} | {p.payer}
        if len(payers) >= 5 and (p.amount >= 500 * R or sum(h.amount for h in inbound) + p.amount >= 5_000 * R):
            hits.append(("Contraparte", "MULE_FAN_IN", 40 if p.payee_pf else 20))
    days_paid = {local(h.t).date() for h in paid_payee_before if h.t <= t - timedelta(hours=24)}
    if len(days_paid) >= 3 and max(h.t for h in paid_payee_before) >= t - timedelta(days=60):
        hits.append(("Contraparte", "RECURRING_PAYEE", -20))
    if same_holder and dest_age < timedelta(days=7):
        hits.append(("Contraparte", "SELF_TRANSFER_NEW_ACCOUNT", 15))
    st = dict_stats(p.payee, t, m)
    if st["state"] == "unavailable":
        hits.append(("Contraparte", "COUNTERPARTY_CHECK_UNAVAILABLE", 10))
    elif st["mule_scammer_d90"] >= 1 and st["reporters_d90"] >= 2:
        floors.append((REJECTED, "COUNTERPARTY_RESTRICTED"))
    elif st["open_reports"] >= 1 or st["markers_d90"] >= 1 or st["markers_m12"] >= 1:
        hits.append(("Contraparte", "COUNTERPARTY_REPORTED", 30))

    # ── Acesso ──
    devices_seen = {h.device for h in mine}
    if devices_seen and p.device not in devices_seen and p.device not in m.authorized_devices.get(p.payer, set()):
        hits.append(("Acesso", "DEVICE_CHANGE", 20))
        new_dev_24h = sum(h.amount for h in mine if exposure(h) and h.t > t - timedelta(hours=24)
                          and h.device not in m.authorized_devices.get(p.payer, set())
                          and h.device not in {x.device for x in mine if x.t < h.t}) + p.amount
        if p.amount > 200 * R or new_dev_24h > 1_000 * R:
            floors.append((TWO_FA, "DEVICE_CHANGE"))
    if any(t - timedelta(hours=24) <= r <= t for r in m.recoveries.get(p.payer, [])):
        hits.append(("Acesso", "RECENT_ACCOUNT_RECOVERY", 25))

    # ── Composição ──
    if same_holder:  # mesma titularidade: Perfil e Montante valem metade
        hits = [(f, c, pts // 2 if f in ("Perfil", "Montante") else pts) for f, c, pts in hits]
    fam: dict = {}
    for f, c, pts in hits:
        fam[f] = fam.get(f, 0) + pts
    fam = {f: min(CAPS[f], max(0, v)) for f, v in fam.items()}
    score = sum(fam.values())
    band = REJECTED if score >= REJECT_AT else TWO_FA if score >= TWO_FA_AT else APPROVED
    status = max([band] + [s for s, _ in floors])
    codes = [f"{c}{'+' if pts > 0 else ''}{pts}" for _, c, pts in hits] + [f"piso:{c}->{STATUS[s]}" for s, c in floors]
    return status, score, fam, codes


# ── Ferramentas de cenário ───────────────────────────────────────────────
BASE = datetime(2026, 10, 17, 14, 0, tzinfo=TZ)
OLD = datetime(2023, 1, 10, 9, 0, tzinfo=TZ)


def at(day_offset: int, hh: int, mm: int = 0) -> datetime:
    return (BASE + timedelta(days=day_offset)).replace(hour=hh, minute=mm)


class Run:
    def __init__(self, title: str, mocks: Optional[Mocks] = None) -> None:
        self.title, self.m, self.hist, self.n = title, mocks or Mocks(), [], 0
        print(f"\n=== {title}")

    def send(self, payer, payee, reais, t, opened=OLD, account=None, device="dev-1", quiet=False, tfa=None):
        self.n += 1
        p = Pix(f"pix-{self.n}", payer, payee, int(reais * R), t, account or f"acc-{payee}", opened, device)
        status, score, fam, codes = evaluate(p, self.hist, self.m)
        p.status, p.tfa = status, (tfa if status == TWO_FA else None)
        self.hist.append(p)
        if not quiet:
            fams = " ".join(f"{k}={v}" for k, v in fam.items() if v)
            print(f"  {local(t):%d/%m %H:%M} R${reais:>8,.0f} -> {STATUS[status]:<20} score={score:<3} {fams} | {', '.join(codes)}")
        return status

    def profile(self, payer, n=6, max_reais=400, start=-10):
        """Histórico de fundo: n Pix aprovados em dias distintos, o maior de max_reais."""
        values = [80, 150, 220, 120, 300, max_reais][:n]
        for i, v in enumerate(values):
            self.send(payer, f"9000000000{i}", v, at(start + i, 12), quiet=True)


A, B, C = "12345678909", "98765432100", "52998224725"   # CPFs válidos
LANDLORD, SELLER = "11144477735", "39053344705"

if __name__ == "__main__":
    r = Run("Exemplo oficial (22h15, R$1.500, destino aberto há 7 dias, primeiro contato)")
    ex_opened = datetime(2026, 10, 10, 9, 0, tzinfo=TZ)
    r.send(A, B, 1500, at(0, 22, 15), opened=ex_opened)
    r.send(A, B, 1500, at(0, 22, 15), opened=ex_opened)  # banca reenvia com id novo
    r.send(A, B, 1500, at(0, 22, 15), opened=ex_opened)

    r = Run("Mesmo payload às 14h")
    r.send(A, B, 1500, at(0, 14), opened=ex_opened)

    r = Run("CPF da banca, desconhecido: R$50 às 14h para conta antiga")
    r.send(C, B, 50, at(0, 14))

    r = Run("Aluguel: 1º mês pede 2FA, cliente confirma (webhook), meses seguintes passam; 6º mês às 22h")
    r.profile(A, start=-200)
    for d in [-150, -120, -90, -60, -30, 0]:
        r.send(A, LANDLORD, 2500, at(d, 10), tfa="passed")
    r.send(A, LANDLORD, 2500, at(30, 22))

    r = Run("Aluguel sem webhook: 2FA nunca confirmado, o motor não aprende")
    r.profile(A, start=-200)
    for d in [-90, -60, -30]:
        r.send(A, LANDLORD, 2500, at(d, 10))

    r = Run("Fracionamento na virada da noite: R$900 às 23h50 + R$900 às 0h10")
    r.profile(A)
    r.send(A, SELLER, 900, at(0, 23, 50))
    r.send(A, SELLER, 900, at(1, 0, 10))

    r = Run("Tomada de conta: aparelho novo + destino de 3 dias + 7x o maior Pix (14h)")
    r.profile(A)
    r.send(A, SELLER, 2800, at(0, 14), opened=at(-3, 9), device="dev-novo")
    m = Mocks(authorized_devices={A: {"dev-s24"}})
    r = Run("Idem, com o aparelho novo autorizado no mock de cadastro", m)
    r.profile(A)
    r.send(A, SELLER, 2800, at(0, 14), opened=at(-3, 9), device="dev-s24")

    r = Run("Conta segura: R$5.000 para conta própria aberta há 2 dias — de dia e à noite")
    r.profile(A)
    r.send(A, A, 5000, at(0, 14), opened=at(-2, 9), account="acc-A-nova")
    r = Run("(à noite)")
    r.profile(A)
    r.send(A, A, 5000, at(0, 23), opened=at(-2, 9), account="acc-A-nova")

    r = Run("Coação: R$5.000 às 23h para favorecido novo com conta antiga (laranja)")
    r.profile(A)
    r.send(A, SELLER, 5000, at(0, 23))
    r = Run("Coação: 4 x R$1.000 às 23h para quatro contas antigas diferentes")
    r.profile(A)
    for i in range(4):
        r.send(A, f"5555555550{i}", 1000, at(0, 23, 5 * i), tfa="passed")

    r = Run("Conta-mula recebendo: destino de 10 dias, 6º pagador em 24h, R$800")
    for i in range(5):
        r.send(f"7777777770{i}", SELLER, 800, at(0, 9 + i), opened=at(-10, 9), quiet=True)
    r.send(C, SELLER, 800, at(0, 15), opened=at(-10, 9))
    m = Mocks(dict_markers={SELLER: [(at(0, 12), "MULE", "psp-1"), (at(0, 13), "MULE", "psp-2")]})
    r = Run("Idem, com marcações de mula de 2 PSPs no mock do DICT", m)
    for i in range(5):
        r.send(f"7777777770{i}", SELLER, 800, at(0, 9 + i), opened=at(-10, 9), quiet=True)
    r.send(C, SELLER, 800, at(0, 15), opened=at(-10, 9))

    r = Run("Rateio: 5 amigos x R$40 para conta antiga")
    for i in range(5):
        r.send(f"6666666660{i}", SELLER, 40, at(0, 20, i), quiet=i < 4)

    r = Run("Estruturação: 3 x R$990 no mesmo dia, pagador sem histórico")
    for h in (9, 13, 18):
        r.send(C, f"4444444440{h}", 990, at(0, h))

    r = Run("Rajada: 10 Pix de R$50 em 9 minutos para pessoas diferentes")
    for i in range(10):
        r.send(C, f"3333333330{i}", 50, at(0, 15, i), quiet=i not in (3, 4, 9))

    r = Run("Folha PJ: 20 salários de R$2.500 em 10 minutos, mês 1 e mês 2")
    EMPRESA = "11222333000181"
    for mes in (-30, 0):
        out = []
        for i in range(20):
            out.append(r.send(EMPRESA, f"2222222220{i:02d}", 2500, at(mes, 9) + timedelta(seconds=30 * i), quiet=True))
        print(f"  mês {mes:>3}: " + ", ".join(f"{STATUS[s]}x{out.count(s)}" for s in sorted(set(out))))

    r = Run("Mock do DICT fora do ar", Mocks(dict_down=True))
    r.send(C, B, 50, at(0, 14))
    r.send(A, B, 1500, at(0, 14), opened=ex_opened)
