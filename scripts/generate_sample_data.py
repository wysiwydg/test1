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

It also carries *structure*, which is the part a book of independently sampled
parties cannot exercise. People are generated into **households** -- a surname
and an address shared by a couple and their children -- and legal entities are
generated **attached to** those households or to a set of directors, rather than
floating free:

*   a family trust sits at its family's address and owns policies on its members;
*   an estate belongs to one deceased person and owns the policy on that person;
*   a company sits at a business address and insures two or three directors who
    live in different households.

Two adversarial cases are deliberate. **Flatmates** share an address with no
family relationship and no shared policy, so anything inferring a household from
address alone gets them wrong. **Married-in** members carry a different surname
at the same address, so anything requiring a surname match misses them. Both are
counted in the report the generator prints, which is what makes the household
derivation measurable rather than merely plausible.

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
    # Every life administration system records why an owner is allowed to own a
    # policy on someone else's life -- insurable interest is a condition of
    # issue, so it is captured at application and kept. It is the strongest
    # household signal a policy extract carries, and inferring what the source
    # already states would be the wrong way round.
    "OwnerRelationshipToInsured",
    "InsuredCustomerId", "InsuredName", "InsuredDOB", "InsuredGender",
    "InsuredEmail", "InsuredPhone", "InsuredAddress1", "InsuredAddress2",
    "InsuredCity", "InsuredPostcode", "InsuredCountry", "InsuredOccupation",
    "AgentCode", "AgentName", "AgentEmail", "AgentPhone",
]

GIVEN_M = ["John", "Michael", "David", "James", "Robert", "José", "Wei",
           "Ahmed", "Thomas", "Daniel", "Christopher", "Anthony", "Nicholas",
           "Gregory", "Alexander", "Matthew", "Peter", "Andrew", "Joseph",
           "Simon", "Martin", "Paul"]
GIVEN_F = ["Katherine", "Sarah", "Emma", "Olivia", "Sophie", "Ana", "Priya",
           "Elizabeth", "Rebecca", "Margaret", "Jennifer", "Patricia",
           "Deborah", "Christina", "Susan", "Laura", "Rachel", "Helen"]
CHILD_M = ["Oliver", "Harry", "Jack", "Charlie", "Leo", "Noah", "Arthur"]
CHILD_F = ["Amelia", "Isla", "Ava", "Mia", "Grace", "Freya", "Ruby"]
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
BUSINESS_STREETS = ["Commercial Road", "Exchange Square", "Kings Wharf"]
CITIES = ["London", "Manchester", "Bristol", "Leeds", "Cardiff"]
OCCUPATIONS = ["Engineer", "Teacher", "Nurse", "Accountant", "Driver", ""]

#: How an owner relates to the life insured, as the source states it.
SELF = "SELF"
SPOUSE = "SPOUSE"
CHILD = "CHILD"
PARENT = "PARENT"
EMPLOYER = "EMPLOYER"
TRUSTEE = "TRUSTEE"
EXECUTOR = "EXECUTOR"


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


def _address(rng: random.Random, streets: list[str]) -> dict[str, str | int]:
    return {
        "street_no": rng.randrange(1, 200),
        "street": rng.choice(streets),
        "city": rng.choice(CITIES),
        "postcode": f"{rng.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}"
                    f"{rng.randrange(1, 20)} {rng.randrange(1, 10)}"
                    f"{rng.choice('ABCDEFGH')}{rng.choice('ABCDEFGH')}",
    }


def _phone(rng: random.Random) -> str:
    return f"{rng.randrange(1000000000, 9999999999)}"[:10]


# ---------------------------------------------------------------------------
# The population
# ---------------------------------------------------------------------------


def _build_population(rng: random.Random, households_wanted: int) -> dict:
    """Households, their members, and the legal entities attached to them.

    A household is an address and a surname shared by up to two adults and
    their children. Everything structural in the extract hangs off this: who
    owns a policy on whom, which trust holds it, and which of the people at one
    address are actually a family.
    """
    people: list[dict] = []
    households: list[dict] = []
    entities: list[dict] = []
    next_person = 0

    def add_person(**kw) -> dict:
        nonlocal next_person
        person = {"id": next_person, "key": f"C-{next_person:06d}",
                  "middle": rng.choice(MIDDLE), "phone": _phone(rng),
                  "occupation": rng.choice(OCCUPATIONS), "deceased": False, **kw}
        people.append(person)
        next_person += 1
        return person

    for h in range(households_wanted):
        surname = rng.choice(SURNAME)
        address = _address(rng, STREETS)
        household = {"id": h, "surname": surname, "address": address,
                     "members": [], "kind": "FAMILY"}

        head = add_person(
            given=rng.choice(GIVEN_M if rng.random() < 0.5 else GIVEN_F),
            surname=surname, address=address, household=h,
            dob=date(1945, 1, 1) + timedelta(days=rng.randrange(0, 14000)),
            gender=rng.choice(["M", "F"]), generation="ADULT",
        )
        household["members"].append(head)

        # A partner, in about half of households. One in five keeps their own
        # surname, which is the case that breaks surname-based householding and
        # is exactly why the stated relationship is carried in the extract.
        if rng.random() < 0.55:
            partner_surname = surname if rng.random() < 0.8 else rng.choice(SURNAME)
            partner = add_person(
                given=rng.choice(GIVEN_F if head["gender"] == "M" else GIVEN_M),
                surname=partner_surname, address=address, household=h,
                dob=head["dob"] + timedelta(days=rng.randrange(-3000, 3000)),
                gender="F" if head["gender"] == "M" else "M",
                generation="ADULT", married_in=partner_surname != surname,
            )
            household["members"].append(partner)
            household["partner"] = partner
        household["head"] = head

        for _ in range(rng.choice([0, 0, 1, 1, 2, 3])):
            child = add_person(
                given=rng.choice(CHILD_M if rng.random() < 0.5 else CHILD_F),
                surname=surname, address=address, household=h,
                dob=head["dob"] + timedelta(days=rng.randrange(8000, 16000)),
                gender=rng.choice(["M", "F"]), generation="CHILD",
            )
            household["members"].append(child)

        households.append(household)

    # Flatmates: a second, unrelated adult moved into an existing address. No
    # shared surname, no shared policy, no family relationship -- so a household
    # inferred from address alone will wrongly swallow them, and one built on
    # evidence will not.
    flatmates = []
    for household in rng.sample(households, k=max(1, len(households) // 12)):
        lodger = add_person(
            given=rng.choice(GIVEN_M + GIVEN_F), surname=rng.choice(SURNAME),
            address=household["address"], household=None,
            dob=date(1960, 1, 1) + timedelta(days=rng.randrange(0, 12000)),
            gender=rng.choice(["M", "F"]), generation="ADULT", flatmate=True,
        )
        flatmates.append(lodger)

    # Legal entities, each attached to what it exists for.
    for household in households:
        roll = rng.random()
        if roll < 0.10:
            entities.append({
                "key": f"ORG-T{household['id']:05d}",
                "name": rng.choice(["{s} Family Trust", "The {s} Trust"]).format(
                    s=household["surname"]),
                "kind": "TRUST", "address": household["address"],
                "household": household["id"], "insures": household["members"],
                "relationship": TRUSTEE,
            })
        elif roll < 0.15 and household["members"]:
            deceased = household["members"][0]
            deceased["deceased"] = True
            entities.append({
                "key": f"ORG-E{household['id']:05d}",
                "name": f"Estate of {deceased['given']} {deceased['surname']}",
                "kind": "ESTATE", "address": household["address"],
                "household": household["id"], "insures": [deceased],
                "relationship": EXECUTOR,
            })

    # Companies: a business address, and directors drawn from several different
    # households. Key-man cover is the case where a party's affiliation is
    # emphatically not a household -- the directors do not live together.
    adults = [p for p in people if p["generation"] == "ADULT"]
    for c in range(max(1, households_wanted // 25)):
        directors = rng.sample(adults, k=min(len(adults), rng.choice([2, 2, 3])))
        entities.append({
            "key": f"ORG-C{c:05d}",
            "name": rng.choice(["{s} Holdings Ltd", "{s} & Sons Pty Ltd",
                                "{s} Group plc"]).format(
                s=rng.choice(directors)["surname"]),
            "kind": "COMPANY", "address": _address(rng, BUSINESS_STREETS),
            "household": None, "insures": directors, "relationship": EMPLOYER,
        })

    return {"people": people, "households": households, "entities": entities,
            "flatmates": flatmates}


def _party_pair(rng: random.Random, population: dict) -> tuple[dict, dict, str]:
    """Choose who owns this policy and whose life it covers.

    Weighted to produce the structure a householding pass has to find, and the
    counter-examples it has to refuse: most policies are owned by the life
    insured, the rest by a spouse, a parent, an adult child, a trust, an estate
    or an employer.
    """
    households = population["households"]
    entities = population["entities"]
    roll = rng.random()

    if roll < 0.14 and entities:
        entity = rng.choice(entities)
        return entity, rng.choice(entity["insures"]), entity["relationship"]

    household = rng.choice(households)
    members = household["members"]
    adults = [m for m in members if m["generation"] == "ADULT"]
    children = [m for m in members if m["generation"] == "CHILD"]

    if roll < 0.34 and len(adults) == 2:
        # Spouses insure each other, in both directions.
        first, second = rng.sample(adults, 2)
        return first, second, SPOUSE
    if roll < 0.46 and adults and children:
        return rng.choice(adults), rng.choice(children), CHILD
    if roll < 0.52 and adults and children:
        # An adult child owning cover on an elderly parent.
        return rng.choice(children), rng.choice(adults), PARENT

    if roll < 0.56 and population["flatmates"]:
        # A flatmate's own policy. Same address as a family, nothing to do with
        # them, and the household pass must leave them out of it.
        person = rng.choice(population["flatmates"])
        return person, person, SELF

    person = rng.choice(members)
    return person, person, SELF


def generate(rows: int, seed: int = 20240807) -> list[dict[str, str]]:
    """Build the extract.

    Parties are sampled *with replacement* from a fixed population, so the same
    person genuinely recurs across policies and across roles. Without that,
    entity resolution would have nothing to find and the sample would flatter
    the system.
    """
    rng = random.Random(seed)
    population = _build_population(rng, households_wanted=max(4, rows // 6))

    agents = [
        {"code": f"AGT-{1000 + i}",
         "name": f"{rng.choice(GIVEN_M + GIVEN_F)} {rng.choice(SURNAME)}"
                 if i % 3 else f"{rng.choice(SURNAME)} Insurance Brokers Pty Ltd"}
        for i in range(max(2, rows // 20))
    ]

    out: list[dict[str, str]] = []
    for n in range(rows):
        owner, insured, relationship = _party_pair(rng, population)
        agent = rng.choice(agents)
        owner_is_entity = "insures" in owner

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
        base_no = 100000 + n
        policy_number = [
            f"POL-{base_no:08d}", f"pol {base_no}", f"POL{base_no}"
        ][rng.randrange(3)]

        def party_cols(prefix: str, p: dict, name: str, dob: str,
                       gender: str) -> dict[str, str]:
            blank = rng.random()
            address = p["address"]
            return {
                f"{prefix}Name": name,
                f"{prefix}DOB": dob,
                f"{prefix}Gender": gender,
                # Person-unique, because real email addresses are. An address
                # derived from the name alone would hand two different people
                # with the same name an identical email, which no matcher can
                # see past -- it would measure the generator, not the matcher.
                f"{prefix}Email": "" if blank < 0.25 else (
                    f"{p.get('given', 'contact').lower()}."
                    f"{p.get('surname', 'admin').lower()}{p.get('id', 0)}@example.com"
                    .replace("'", "").replace("é", "e").replace("ñ", "n")
                    .replace("ü", "u").replace(" ", "")),
                f"{prefix}Phone": "" if blank < 0.15 else
                    _fmt_phone(p.get("phone") or "2079460000", rng.randrange(4)),
                f"{prefix}Address1": f"{address['street_no']} {address['street']}",
                f"{prefix}Address2": "" if rng.random() < 0.8
                    else f"Flat {rng.randrange(1, 12)}",
                f"{prefix}City": address["city"],
                f"{prefix}Postcode": address["postcode"] if rng.random() < 0.7
                    else address["postcode"].replace(" ", "").lower(),
                f"{prefix}Country": "GB",
                f"{prefix}Occupation": p.get("occupation", ""),
            }

        if owner_is_entity:
            owner_key, owner_name = owner["key"], owner["name"]
            owner_dob = owner_gender = ""
        else:
            owner_key = owner["key"]
            owner_name = _vary_name(rng, owner["given"], owner["middle"],
                                    owner["surname"])
            owner_dob = _fmt_date(owner["dob"], ds)
            owner_gender = owner["gender"]

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
            "PremiumFrequency": rng.choice(["M", "A", "Q", "MONTHLY",
                                            "ANNUAL", "S"]),
            "ApplicationDate": _fmt_date(app, ds),
            "IssueDate": _fmt_date(issue, ds),
            "EffectiveDate": _fmt_date(eff, ds),
            "MaturityDate": _fmt_date(eff + timedelta(days=365 * 20), ds),
            "TerminationDate": _fmt_date(
                eff + timedelta(days=rng.randrange(400, 4000)), ds
            ) if terminated else "",
            "PaidToDate": _fmt_date(eff + timedelta(days=365), ds),
            "SumAssured": _fmt_money(sum_assured, ms),
            "AnnualPremium": _fmt_money(annual, ms),
            "ModalPremium": _fmt_money(annual / 12, ms),
            "AccountValue": _fmt_money(annual * rng.uniform(1, 15), ms),
            "PolicyTerm": str(rng.choice([10, 15, 20, 25, 30])),
            "PremiumTerm": str(rng.choice([10, 15, 20])),
            "LastUpdatedTs": (eff + timedelta(days=rng.randrange(1, 3000))).isoformat(),
            "OwnerCustomerId": owner_key,
            # Stated blank on some rows, because it is on some rows of every
            # real extract, and a derivation that only works where the source
            # filled the field in is a derivation that does not work.
            "OwnerRelationshipToInsured": "" if rng.random() < 0.08 else relationship,
            "InsuredCustomerId": insured["key"],
            "AgentCode": agent["code"],
            "AgentName": agent["name"],
            "AgentEmail": f"{agent['code'].lower()}@broker.example.com",
            "AgentPhone": _fmt_phone(_phone(rng), 1),
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


def describe(rows: int, seed: int) -> dict[str, int]:
    """The structure the extract contains, as ground truth.

    Printed on generation and asserted in tests. A householding pass measured
    against nothing is a householding pass that cannot be wrong.
    """
    rng = random.Random(seed)
    population = _build_population(rng, households_wanted=max(4, rows // 6))
    households = population["households"]
    multi = [h for h in households if len(h["members"]) > 1]
    return {
        "people": len(population["people"]),
        "households": len(households),
        "households_with_2_or_more": len(multi),
        "largest_household": max((len(h["members"]) for h in households), default=0),
        "married_in_members": sum(
            1 for p in population["people"] if p.get("married_in")
        ),
        "flatmates": len(population["flatmates"]),
        "trusts": sum(1 for e in population["entities"] if e["kind"] == "TRUST"),
        "estates": sum(1 for e in population["entities"] if e["kind"] == "ESTATE"),
        "companies": sum(1 for e in population["entities"] if e["kind"] == "COMPANY"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20240807)
    parser.add_argument("--out", type=pathlib.Path,
                        default=REPO / "data" / "life_admin_sample.csv")
    args = parser.parse_args(argv)

    rows = generate(args.rows, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {args.out} ({len(rows)} rows, {len(COLUMNS)} columns)")
    print("\nground truth in this extract:")
    for name, value in describe(args.rows, args.seed).items():
        print(f"  {name.replace('_', ' '):28} {value:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
