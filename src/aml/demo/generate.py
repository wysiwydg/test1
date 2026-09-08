"""Generate a demonstration book with typologies planted in known places.

Every AML system claims to detect structuring. The useful question is whether
*this* one does, on data where the answer is known, and that is what this
module is for: it writes an extract containing ordinary premium traffic plus
one deliberate instance of each typology the rule set claims to cover, and
:data:`PLANTED` records where each one is. The test suite asserts that the
rules find them; a demonstration to a compliance committee shows the same thing
with the answer key in hand.

The data is synthetic. The names are constructed from common Philippine name
components and belong to nobody; the sanctions list is a fabricated file with a
made-up designated person on it, for the same reason a screening demonstration
must never use a real designation.

Deterministic under a seed, so the demo produces the same book every time and a
change in output means a change in behaviour.
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
import random
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["generate", "PLANTED"]

#: What is hidden in the extract, and which rule should find it.
PLANTED: Mapping[str, str] = {
    "C-0001": "str.structuring — three cash payments of 480,000 in four days, three branches",
    "C-0002": "str.early_surrender — 2,500,000 single premium cancelled inside the free-look",
    "C-0003": "str.third_party_payer — premium settled by an unrelated Singapore company",
    "C-0004": "screening.sanctions_match / str.designated_party — matches the designated list",
    "C-0005": "str.high_risk_jurisdiction — surrender proceeds to a call-for-action country",
    "C-0006": "str.overpayment_refund — overpayment refunded to a different account",
    "C-0007": "str.rapid_movement — 1,200,000 top-up withdrawn within nine days",
    "C-0008": "str.income_capacity — 3,000,000 paid against a declared income of 480,000",
    "C-0009": "str.beneficiary_churn — beneficiary changed twelve days before a payout",
    "C-0010": "ctr.single_transaction — 750,000 in cash, and a USD single premium over the "
              "threshold once converted",
}

_FIRST = [
    "Juan", "Maria", "Jose", "Ana", "Antonio", "Rosario", "Ricardo", "Teresa", "Eduardo",
    "Corazon", "Manuel", "Luzviminda", "Roberto", "Imelda", "Fernando", "Carmen", "Alfredo",
    "Josefina", "Rodolfo", "Bernadette", "Ramon", "Cristina", "Emilio", "Aurora",
]
_MIDDLE = ["Santos", "Reyes", "Bautista", "Garcia", "Mendoza", "Cruz", "Aquino", "Villanueva"]
_LAST = [
    "Dela Cruz", "Reyes", "Santos", "Bautista", "Ocampo", "Mercado", "Tolentino", "Aguilar",
    "Ramos", "Del Rosario", "Fernandez", "Lim", "Tan", "Sy", "Gonzales", "Pascual",
]
_OCCUPATIONS = [
    ("Teacher", 480_000), ("Nurse", 620_000), ("Overseas worker", 900_000),
    ("Business owner", 3_500_000), ("Physician", 4_200_000), ("Engineer", 1_100_000),
    ("Government employee", 720_000), ("Farmer", 260_000), ("Call centre agent", 420_000),
]
_CITIES = [
    ("Makati", "Metro Manila"), ("Quezon City", "Metro Manila"), ("Cebu City", "Cebu"),
    ("Davao City", "Davao del Sur"), ("Iloilo City", "Iloilo"), ("Baguio", "Benguet"),
]
_BRANCHES = ["MNL-01", "MNL-02", "MNL-03", "CEB-01", "DVO-01"]


def _write(path: pathlib.Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def generate(
    out_dir: str | pathlib.Path = "data/aml-demo",
    *,
    customers: int = 60,
    seed: int = 20260908,
    start: dt.date = dt.date(2026, 1, 5),
) -> dict[str, pathlib.Path]:
    """Write parties, policies, transactions, a watch list and FX rates."""
    rng = random.Random(seed)
    base = pathlib.Path(out_dir)

    parties: list[dict[str, Any]] = []
    policies: list[dict[str, Any]] = []
    transactions: list[dict[str, Any]] = []

    def add_party(index: int, **overrides: Any) -> str:
        party_id = f"C-{index:04d}"
        occupation, income = rng.choice(_OCCUPATIONS)
        city, province = rng.choice(_CITIES)
        row: dict[str, Any] = {
            "party_id": party_id,
            "party_type": "INDIVIDUAL",
            "full_name": f"{rng.choice(_FIRST)} {rng.choice(_MIDDLE)} {rng.choice(_LAST)}",
            "birth_date": dt.date(
                rng.randint(1955, 1998), rng.randint(1, 12), rng.randint(1, 28)
            ).isoformat(),
            "nationality": "PH",
            "country_of_residence": "PH",
            "address_line": f"{rng.randint(1, 250)} {rng.choice(_LAST)} Street",
            "city": city,
            "province": province,
            "postal_code": f"{rng.randint(1000, 6999)}",
            "occupation": occupation,
            "declared_income": income,
            "source_of_funds": "Salary",
            "pep_status": "NOT_PEP",
            "risk_rating": "NORMAL",
            "customer_since": (start - dt.timedelta(days=rng.randint(200, 3000))).isoformat(),
            "id_type": "PHILSYS",
            "id_number": f"{rng.randint(1000, 9999)}-{rng.randint(1000, 9999)}-"
                         f"{rng.randint(1000, 9999)}",
            "is_frozen": "",
            "source_system": "POLICY_ADMIN",
        }
        row.update(overrides)
        parties.append(row)
        return party_id

    def add_policy(party_id: str, index: int, **overrides: Any) -> str:
        policy_id = f"PL-{index:05d}"
        row: dict[str, Any] = {
            "policy_id": policy_id,
            "policy_number": f"VUL-{index:05d}",
            "product_line": rng.choice(
                ["TRADITIONAL_LIFE", "VARIABLE_UNIT_LINKED", "SINGLE_PREMIUM"]
            ),
            "product_name": "Kabuhayan Protect",
            "currency": "PHP",
            "sum_assured": rng.choice([500_000, 1_000_000, 2_000_000, 5_000_000]),
            "is_single_premium": "",
            "inception_date": (start - dt.timedelta(days=rng.randint(30, 1500))).isoformat(),
            "status": "IN_FORCE",
            "owner_party_id": party_id,
            "insured_party_id": party_id,
            "agent_code": f"AG-{rng.randint(100, 199)}",
            "branch_code": rng.choice(_BRANCHES),
            "free_look_days": 15,
            "source_system": "POLICY_ADMIN",
        }
        row.update(overrides)
        policies.append(row)
        return policy_id

    def add_txn(**fields: Any) -> None:
        transactions.append(
            {
                "txn_id": f"T-{len(transactions) + 1:06d}",
                "source_reference": f"OR-{100000 + len(transactions)}",
                "currency": "PHP",
                "direction": "INBOUND",
                "channel": "BRANCH",
                "source_system": "POLICY_ADMIN",
                **fields,
            }
        )

    # -- the ten planted cases ------------------------------------------
    structurer = add_party(1, full_name="Rodrigo Santos Villanueva", occupation="Business owner",
                           declared_income=3_500_000)
    policy = add_policy(structurer, 1)
    for offset, branch in enumerate(("MNL-01", "MNL-02", "CEB-01")):
        add_txn(
            occurred_at=f"{(start + dt.timedelta(days=10 + offset)).isoformat()}T10:15:00",
            party_id=structurer, policy_id=policy, amount=480_000,
            txn_type="PREMIUM_PAYMENT", instrument="CASH", branch_code=branch,
        )

    washer = add_party(2, full_name="Bienvenido Cruz Alcantara", occupation="Business owner",
                       declared_income=5_000_000)
    wash_policy = add_policy(washer, 2, is_single_premium="Y", product_line="SINGLE_PREMIUM")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=3)).isoformat()}T09:00:00",
            party_id=washer, policy_id=wash_policy, amount=2_500_000,
            txn_type="SINGLE_PREMIUM", instrument="MANAGERS_CHECK", branch_code="MNL-01")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=14)).isoformat()}T14:30:00",
            party_id=washer, policy_id=wash_policy, amount=2_500_000, direction="OUTBOUND",
            txn_type="FREE_LOOK_CANCELLATION", instrument="BANK_TRANSFER", branch_code="MNL-01")

    fronted = add_party(3, full_name="Milagros Reyes Bautista", occupation="Nurse",
                        declared_income=620_000)
    fronted_policy = add_policy(fronted, 3)
    add_txn(occurred_at=f"{(start + dt.timedelta(days=21)).isoformat()}T11:00:00",
            party_id=fronted, policy_id=fronted_policy, amount=800_000,
            txn_type="SINGLE_PREMIUM", instrument="SWIFT_TRANSFER", channel="BANCASSURANCE",
            counterparty_name="Orient Star Holdings Pte Ltd", counterparty_country="SG",
            counterparty_bank="United Overseas Bank", counterparty_account="SG-99881122")

    designated = add_party(4, full_name="Faisal Ahmad Rahman", nationality="PH",
                           birth_date="1979-06-22", is_frozen="Y", risk_rating="HIGH",
                           occupation="Trader", declared_income=900_000)
    designated_policy = add_policy(designated, 4)
    add_txn(occurred_at=f"{(start + dt.timedelta(days=30)).isoformat()}T15:45:00",
            party_id=designated, policy_id=designated_policy, amount=250_000,
            txn_type="PREMIUM_PAYMENT", instrument="CASH", branch_code="DVO-01")

    offshore = add_party(5, full_name="Gregorio Lim Ocampo", occupation="Engineer",
                         declared_income=1_100_000)
    offshore_policy = add_policy(offshore, 5)
    add_txn(occurred_at=f"{(start + dt.timedelta(days=40)).isoformat()}T10:00:00",
            party_id=offshore, policy_id=offshore_policy, amount=400_000,
            txn_type="PREMIUM_PAYMENT", instrument="BANK_TRANSFER")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=95)).isoformat()}T10:00:00",
            party_id=offshore, policy_id=offshore_policy, amount=380_000, direction="OUTBOUND",
            txn_type="PARTIAL_WITHDRAWAL", instrument="SWIFT_TRANSFER",
            counterparty_name="Pars Trading Co", counterparty_country="IR",
            counterparty_account="IR-4455")

    refunder = add_party(6, full_name="Estrella Garcia Pascual", occupation="Business owner",
                         declared_income=2_800_000)
    refund_policy = add_policy(refunder, 6)
    add_txn(occurred_at=f"{(start + dt.timedelta(days=18)).isoformat()}T09:30:00",
            party_id=refunder, policy_id=refund_policy, amount=900_000,
            txn_type="PREMIUM_PAYMENT", instrument="BANK_TRANSFER",
            counterparty_name="Estrella G. Pascual", counterparty_account="BDO-1122334455",
            counterparty_relationship="SELF")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=33)).isoformat()}T16:00:00",
            party_id=refunder, policy_id=refund_policy, amount=600_000, direction="OUTBOUND",
            txn_type="OVERPAYMENT_REFUND", instrument="BANK_TRANSFER",
            counterparty_name="RGP Ventures Inc", counterparty_account="SEC-9090909090")

    passer = add_party(7, full_name="Nestor Aquino Tolentino", occupation="Physician",
                       declared_income=4_200_000)
    pass_policy = add_policy(passer, 7, product_line="VARIABLE_UNIT_LINKED")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=50)).isoformat()}T09:00:00",
            party_id=passer, policy_id=pass_policy, amount=1_200_000,
            txn_type="TOP_UP", instrument="BANK_TRANSFER")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=59)).isoformat()}T09:00:00",
            party_id=passer, policy_id=pass_policy, amount=1_100_000, direction="OUTBOUND",
            txn_type="PARTIAL_WITHDRAWAL", instrument="BANK_TRANSFER")

    stretched = add_party(8, full_name="Lourdes Mendoza Aguilar", occupation="Teacher",
                          declared_income=480_000)
    stretched_policy = add_policy(stretched, 8)
    for offset in (60, 75, 90):
        add_txn(occurred_at=f"{(start + dt.timedelta(days=offset)).isoformat()}T10:00:00",
                party_id=stretched, policy_id=stretched_policy, amount=1_000_000,
                txn_type="PREMIUM_PAYMENT", instrument="BANK_TRANSFER")

    churner = add_party(9, full_name="Alfredo Cruz Del Rosario", occupation="Business owner",
                        declared_income=3_500_000)
    churn_policy = add_policy(churner, 9)
    add_txn(occurred_at=f"{(start + dt.timedelta(days=70)).isoformat()}T10:00:00",
            party_id=churner, policy_id=churn_policy, amount=0, direction="NON_MONETARY",
            txn_type="BENEFICIARY_CHANGE", instrument="OTHER",
            remarks="Beneficiary changed to a non-relative")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=82)).isoformat()}T10:00:00",
            party_id=churner, policy_id=churn_policy, amount=1_500_000, direction="OUTBOUND",
            txn_type="FULL_SURRENDER", instrument="BANK_TRANSFER")

    cash_payer = add_party(10, full_name="Isabelo Ramos Fernandez", occupation="Business owner",
                           declared_income=3_500_000)
    cash_policy = add_policy(cash_payer, 10)
    add_txn(occurred_at=f"{(start + dt.timedelta(days=12)).isoformat()}T13:20:00",
            party_id=cash_payer, policy_id=cash_policy, amount=750_000,
            txn_type="SINGLE_PREMIUM", instrument="CASH", branch_code="MNL-03")
    usd_policy = add_policy(cash_payer, 11, currency="USD", product_line="SINGLE_PREMIUM",
                            is_single_premium="Y")
    add_txn(occurred_at=f"{(start + dt.timedelta(days=26)).isoformat()}T11:10:00",
            party_id=cash_payer, policy_id=usd_policy, amount=20_000, currency="USD",
            txn_type="SINGLE_PREMIUM", instrument="SWIFT_TRANSFER",
            counterparty_name="Isabelo R. Fernandez", counterparty_relationship="SELF",
            counterparty_country="US")

    # -- ordinary traffic ------------------------------------------------
    for index in range(11, customers + 1):
        party_id = add_party(index)
        for policy_index in range(rng.randint(1, 2)):
            policy_id = add_policy(party_id, 100 + index * 2 + policy_index)
            modal = rng.choice([3_500, 8_000, 15_000, 25_000, 42_000])
            for month in range(rng.randint(3, 9)):
                day = start + dt.timedelta(days=30 * month + rng.randint(0, 20))
                clock = f"{rng.randint(9, 16):02d}:{rng.randint(0, 59):02d}:00"
                add_txn(
                    occurred_at=f"{day.isoformat()}T{clock}",
                    party_id=party_id, policy_id=policy_id, amount=modal,
                    txn_type="PREMIUM_PAYMENT",
                    instrument=rng.choice(
                        ["BANK_TRANSFER", "CREDIT_CARD", "AUTO_DEBIT_ARRANGEMENT",
                         "EWALLET", "CASH"]
                    ),
                    channel=rng.choice(["BRANCH", "AGENT", "ONLINE", "PAYMENT_CENTER"]),
                    branch_code=rng.choice(_BRANCHES),
                )

    # -- reference data --------------------------------------------------
    watchlist = [
        {
            "entry_id": "UN-DEMO-001",
            "name": "Faisal Ahmad Rahman",
            "list_type": "UN_DESIGNATED",
            "list_name": "Demonstration designated persons list",
            "aliases": "Faisal A. Rahman;Faysal Rahman",
            "entity_type": "INDIVIDUAL",
            "birth_dates": "1979-06-22",
            "nationalities": "PH",
            "designations": "Demonstration sanctions programme",
            "remarks": "SYNTHETIC ENTRY FOR DEMONSTRATION. Not a real designation.",
            "listed_on": "2024-02-15",
        },
        {
            "entry_id": "PEP-DEMO-002",
            "name": "Corazon Mendoza Tolentino",
            "list_type": "PEP",
            "list_name": "Demonstration PEP list",
            "entity_type": "INDIVIDUAL",
            "positions": "Provincial governor",
            "nationalities": "PH",
            "remarks": "SYNTHETIC ENTRY FOR DEMONSTRATION.",
        },
        {
            "entry_id": "INT-DEMO-003",
            "name": "Orient Star Holdings Pte Ltd",
            "list_type": "INTERNAL_WATCHLIST",
            "list_name": "Internal watchlist",
            "entity_type": "ENTITY",
            "countries": "SG",
            "remarks": "SYNTHETIC ENTRY. Previously reported third-party payer.",
        },
    ]

    rates = [
        {"currency": "USD", "date": (start + dt.timedelta(days=d)).isoformat(),
         "rate": f"{56.10 + (d % 7) * 0.05:.4f}"}
        for d in range(0, 120, 7)
    ]

    paths = {
        "parties": base / "parties.csv",
        "policies": base / "policies.csv",
        "transactions": base / "transactions.csv",
        "watchlist": base / "lists" / "watchlist.csv",
        "rates": base / "rates.csv",
    }
    _write(paths["parties"], parties)
    _write(paths["policies"], policies)
    _write(paths["transactions"], transactions)
    _write(paths["watchlist"], watchlist)
    _write(paths["rates"], rates)
    (base / "ANSWER-KEY.md").write_text(
        "# What is planted in this extract\n\n"
        "Synthetic data. Every name, list entry and transaction is invented.\n\n"
        + "\n".join(f"- **{party}** — {what}" for party, what in PLANTED.items())
        + "\n",
        encoding="utf-8",
    )
    return paths
