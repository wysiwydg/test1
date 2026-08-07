"""Generate a synthetic insurance policy extract for exercising the pipeline.

Clean synthetic data proves nothing. The point of an MDM system is what it does
with data that disagrees with itself, so this generator deliberately injects the
failure modes that real extracts contain:

*   The **same party under different identifiers** across roles and records, so
    that cross-source resolution has something to resolve.
*   **Name variation** on the same person: order swapped, honorifics added,
    accents dropped, a middle name present or absent, a nickname substituted.
*   **Formatting drift** in dates, phone numbers, money and policy numbers.
*   **Non-natural-person owners**: trusts, estates and companies.
*   **Missing values**, because a party block with nothing in it is the normal
    case for an unused role.

Run as ``python -m scripts.generate_sample_data`` to write a CSV under
``data/``. The seed is fixed, so a given invocation produces the same file and
a test can assert against exact counts.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import random
import sys
from datetime import date, timedelta

REPO = pathlib.Path(__file__).resolve().parent.parent

COLUMNS = [
    "PolicyNumber", "ProductCode", "ProductName", "ProductLine", "PlanCode",
    "Status", "IssuingCompany", "BranchCode", "Channel", "IssueState",
    "UwClass", "PaymentMethod", "PremiumFrequency",
    "ApplicationDate", "IssueDate", "EffectiveDate", "MaturityDate",
    "TerminationDate", "PaidToDate",
    "SumAssured", "AnnualPremium", "ModalPremium", "AccountValue",
    "PolicyTerm", "PremiumTerm", "LastUpdatedTs",
    "OwnerCustomerId", "OwnerName", "OwnerDOB", "OwnerGender", "OwnerEmail",
    "OwnerPhone", "OwnerAddress1", "OwnerAddress2", "OwnerCity",
    "OwnerPostcode", "OwnerCountry", "OwnerOccupation",
    "InsuredCustomerId", "InsuredName", "InsuredDOB", "InsuredGender",
    "InsuredEmail", "InsuredPhone", "InsuredAddress1", "InsuredAddress2",
    "InsuredCity", "InsuredPostcode", "InsuredCountry", "InsuredOccupation",
    "AgentCode", "AgentName", "AgentEmail", "AgentPhone",
]

GIVEN = ["John", "Katherine", "Michael", "Sarah", "David", "Emma", "James",
         "Olivia", "Robert", "Sophie", "José", "Ana", "Wei", "Priya", "Ahmed",
         "Thomas", "Elizabeth", "Daniel", "Rebecca", "Christopher", "Margaret",
         "Anthony", "Jennifer", "Nicholas", "Patricia", "Gregory", "Deborah",
         "Alexander", "Christina", "Matthew", "Susan", "Peter", "Andrew",
         "Joseph", "Laura", "Simon", "Rachel", "Martin", "Helen", "Paul"]
MIDDLE = ["Michael", "Anne", "Lee", "Marie", "James", "Rose", ""]
SURNAME = ["Smith", "Phillips", "O'Brien", "Peña", "Nguyen", "Patel", "Müller",
           "Johnson", "Brown", "Taylor", "Wilson", "Davies", "Kowalski",
           "Anderson", "Thompson", "Robinson", "Walker", "Wright", "Hughes",
           "Edwards", "Green", "Hall", "Wood", "Harris", "Clarke", "Jackson",
           "Bennett", "Fletcher", "Morgan", "Hunter", "Sullivan", "Murphy",
           "van der Berg", "de Souza", "Rossi", "Yamamoto", "Okafor", "Ibrahim"]
NICKNAMES = {"John": "Jon", "Katherine": "Kate", "Michael": "Mike",
             "Robert": "Bob", "Sarah": "Sara", "James": "Jim"}
STREETS = ["High Street", "North Avenue", "Church Lane", "Mill Road",
           "Victoria Terrace", "Station Road"]
CITIES = ["London", "Manchester", "Bristol", "Leeds", "Cardiff"]
ORGS = ["{s} Family Trust", "The {s} Trust", "Estate of {g} {s}",
        "{s} Holdings Ltd", "{s} & Sons Pty Ltd"]
OCCUPATIONS = ["Engineer", "Teacher", "Nurse", "Accountant", "Driver", ""]


def _fmt_date(d: date, style: int) -> str:
    """Render a date in one of the source's inconsistent formats."""
    return [d.isoformat(), d.strftime("%d/%m/%Y"), d.strftime("%d-%b-%Y")][style]


def _fmt_money(value: float, style: int) -> str:
    return [f"{value:.2f}", f"£{value:,.2f}", f"{value:,.0f}"][style]


def _fmt_phone(national: str, style: int) -> str:
    return [f"0{national}", f"+44 {national}", f"0044{national}",
            f"0{national[:3]} {national[3:]}"][style]


def _vary_name(rng: random.Random, given: str, middle: str, surname: str) -> str:
    """Render one person's name the way a second system might have keyed it.

    Each variation here is one a real feed produces, and each is one the
    normalization kernels are expected to see through.
    """
    style = rng.randrange(6)
    if style == 0:
        return f"{given} {middle} {surname}".replace("  ", " ").strip()
    if style == 1:
        return f"{surname}, {given}"
    if style == 2:
        return f"{rng.choice(['Mr', 'Mrs', 'Ms', 'Dr'])} {given} {surname}"
    if style == 3:
        return f"{NICKNAMES.get(given, given)} {surname}"
    if style == 4:
        return f"{given} {surname}".upper()
    return f"{given} {middle[:1]} {surname}".replace("  ", " ").strip()


def generate(rows: int, seed: int = 20240807) -> list[dict[str, str]]:
    """Build the extract.

    A pool of parties is created first and then sampled *with replacement*, so
    the same person genuinely recurs across policies and across roles. Without
    that, entity resolution would have nothing to find and the sample would
    flatter the system.
    """
    rng = random.Random(seed)

    people = []
    for i in range(max(4, rows // 3)):
        given = rng.choice(GIVEN)
        people.append({
            "id": i,
            "given": given,
            "middle": rng.choice(MIDDLE),
            "surname": rng.choice(SURNAME),
            "dob": date(1945, 1, 1) + timedelta(days=rng.randrange(0, 20000)),
            "gender": rng.choice(["M", "F"]),
            "street_no": rng.randrange(1, 200),
            "street": rng.choice(STREETS),
            "city": rng.choice(CITIES),
            "postcode": f"{rng.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}"
                        f"{rng.randrange(1, 20)} {rng.randrange(1, 10)}"
                        f"{rng.choice('ABCDEFGH')}{rng.choice('ABCDEFGH')}",
            "phone": f"{rng.randrange(1000000000, 9999999999)}"[:10],
            "occupation": rng.choice(OCCUPATIONS),
        })

    agents = [
        {"code": f"AGT-{1000 + i}",
         "name": f"{rng.choice(GIVEN)} {rng.choice(SURNAME)}" if i % 3 else
                 f"{rng.choice(SURNAME)} Insurance Brokers Pty Ltd"}
        for i in range(max(2, rows // 20))
    ]

    out: list[dict[str, str]] = []
    for n in range(rows):
        owner = rng.choice(people)
        # Most policies are owned by the life insured; a minority are not, and
        # that minority is where owner/insured relationship edges get
        # interesting.
        insured = owner if rng.random() < 0.65 else rng.choice(people)
        agent = rng.choice(agents)

        eff = date(2005, 1, 1) + timedelta(days=rng.randrange(0, 7000))
        issue = eff - timedelta(days=rng.randrange(0, 40))
        app = issue - timedelta(days=rng.randrange(5, 90))
        ds = rng.randrange(3)
        ms = rng.randrange(3)
        status = rng.choice(
            ["In Force", "INFORCE", "Active", "LAPSED", "Surrendered",
             "Matured", "Paid_Up", "ISSUED"]
        )
        terminated = status in ("LAPSED", "Surrendered", "Matured")

        sum_assured = rng.randrange(25_000, 1_000_000, 5_000)
        annual = round(sum_assured * rng.uniform(0.004, 0.02), 2)

        # Policy numbers arrive in several shapes for the same book.
        pol_style = rng.randrange(3)
        base_no = 100000 + n
        policy_number = [
            f"POL-{base_no:08d}", f"pol {base_no}", f"POL{base_no}"
        ][pol_style]

        # A minority of owners are trusts, estates or companies. Such an owner
        # gets its OWN customer id: a trust that owns a policy is a different
        # party from the person insured under it, and giving both the same
        # identifier would assert they are one party -- which would make the
        # ground truth wrong and punish the matcher for correctly refusing to
        # merge a company with a human.
        owner_is_org = rng.random() < 0.12
        owner_key = f"C-{owner['id']:06d}"
        if owner_is_org:
            owner_key = f"ORG-{owner['id']:06d}"
            owner_name = rng.choice(ORGS).format(
                s=owner["surname"], g=owner["given"]
            )
            owner_dob = ""
            owner_gender = ""
        else:
            owner_name = _vary_name(rng, owner["given"], owner["middle"], owner["surname"])
            owner_dob = _fmt_date(owner["dob"], ds)
            owner_gender = owner["gender"]

        def party_cols(prefix: str, p: dict, name: str, dob: str, gender: str) -> dict[str, str]:
            blank = rng.random()
            return {
                f"{prefix}Name": name,
                f"{prefix}DOB": dob,
                f"{prefix}Gender": gender,
                # Person-unique, because real email addresses are. An address
                # derived from the name alone would hand two different people
                # with the same name an identical email, which no matcher can
                # see past -- it would measure the generator, not the matcher.
                f"{prefix}Email": "" if blank < 0.25 else
                    (f"{p['given'].lower()}.{p['surname'].lower()}{p['id']}@example.com"
                     .replace("'", "").replace("é", "e").replace("ñ", "n")
                     .replace("ü", "u")),
                f"{prefix}Phone": "" if blank < 0.15 else _fmt_phone(p["phone"], rng.randrange(4)),
                f"{prefix}Address1": f"{p['street_no']} {p['street']}",
                f"{prefix}Address2": "" if rng.random() < 0.8 else f"Flat {rng.randrange(1, 12)}",
                f"{prefix}City": p["city"],
                f"{prefix}Postcode": p["postcode"] if rng.random() < 0.7
                    else p["postcode"].replace(" ", "").lower(),
                f"{prefix}Country": "GB",
                f"{prefix}Occupation": p["occupation"],
            }

        row = {
            "PolicyNumber": policy_number,
            "ProductCode": rng.choice(["TL10", "WL01", "CI05", "AN20"]),
            "ProductName": rng.choice(["Term Life 10", "Whole of Life",
                                       "Critical Illness Plus", "Annuity 20"]),
            "ProductLine": rng.choice(["LIFE", "TERM_LIFE", "WHOLE_LIFE",
                                       "CRITICAL_ILLNESS", "ANNUITY"]),
            "PlanCode": f"P{rng.randrange(100, 999)}",
            "Status": status,
            "IssuingCompany": rng.choice(["ACME-LIFE", "ACME-UK"]),
            "BranchCode": f"BR{rng.randrange(10, 99)}",
            "Channel": rng.choice(["BROKER", "DIRECT", "BANCASSURANCE"]),
            "IssueState": "GB",
            "UwClass": rng.choice(["STANDARD", "PREFERRED", "SUBSTANDARD", ""]),
            "PaymentMethod": rng.choice(["DD", "CARD", "PAYROLL"]),
            "PremiumFrequency": rng.choice(["M", "A", "Q", "MONTHLY", "ANNUAL", "S"]),
            "ApplicationDate": _fmt_date(app, ds),
            "IssueDate": _fmt_date(issue, ds),
            "EffectiveDate": _fmt_date(eff, ds),
            "MaturityDate": _fmt_date(eff + timedelta(days=365 * 20), ds),
            "TerminationDate": _fmt_date(eff + timedelta(days=rng.randrange(400, 4000)), ds)
                if terminated else "",
            "PaidToDate": _fmt_date(eff + timedelta(days=365), ds),
            "SumAssured": _fmt_money(sum_assured, ms),
            "AnnualPremium": _fmt_money(annual, ms),
            "ModalPremium": _fmt_money(annual / 12, ms),
            "AccountValue": _fmt_money(annual * rng.uniform(1, 15), ms),
            "PolicyTerm": str(rng.choice([10, 15, 20, 25, 30])),
            "PremiumTerm": str(rng.choice([10, 15, 20])),
            "LastUpdatedTs": (eff + timedelta(days=rng.randrange(1, 3000))).isoformat(),
            "OwnerCustomerId": owner_key,
            "InsuredCustomerId": f"C-{insured['id']:06d}",
            "AgentCode": agent["code"],
            "AgentName": agent["name"],
            "AgentEmail": f"{agent['code'].lower()}@broker.example.com",
            "AgentPhone": _fmt_phone(f"{rng.randrange(1000000000, 9999999999)}"[:10], 1),
        }
        row.update(party_cols("Owner", owner, owner_name, owner_dob, owner_gender))
        row.update(party_cols(
            "Insured", insured,
            _vary_name(rng, insured["given"], insured["middle"], insured["surname"]),
            _fmt_date(insured["dob"], rng.randrange(3)),
            insured["gender"],
        ))
        out.append(row)

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20240807)
    parser.add_argument("--out", type=pathlib.Path, default=REPO / "data" / "life_admin_sample.csv")
    args = parser.parse_args(argv)

    rows = generate(args.rows, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {args.out} ({len(rows)} rows, {len(COLUMNS)} columns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
