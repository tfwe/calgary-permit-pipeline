"""Company-name to URL matching must reject lookalikes without losing real sites.

Every case here is a real false positive or false negative seen in production;
add a case whenever a bad match reaches an output CSV.
"""

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
from _harness import P, check, finish

# (page title, url, company, expected)
CASES = [
    # False positives that must stay rejected
    ("Edit workbooks online for free | Microsoft Excel for the Web",
     "https://excel.cloud.microsoft/en-gb/", "EXCEL HOMES LIMITED PARTNERSHIP", False),
    ("Amanda | Amanda the Adventurer Wiki | Fandom",
     "https://amanda-the-adventurer.fandom.com/wiki/Amanda", "HAMILTON DESIGN STUDIO", False),
    ("AI Developers", "https://openhouse.ai/", "AI DEVELOPERS", False),
    ("Base", "https://www.base.org/?1", "PRAIRIE CONSTRUCTION", False),
    ("Formed", "https://app.formed.org/app/login",
     "ALLIANCE ARCHITECTURE GROUP STUDIO", False),
    ("Missing Portfolio", "https://portfolio.adobe.com/missing",
     "ALEPH DESIGN BUILD LAB", False),
    ("Hartwell Homes", "https://www.cityofcalgary.com/contractor/hartwell-homes/",
     "HARTWELL HOMES", False),
    # Real matches that must survive the rejection rules
    ("Hillcrest Construction Management", "https://hillcrestbuild.ca/",
     "HILLCREST CONSTRUCTION MANAGEMENT", True),
    ("Canyon Construction", "https://canyonbuild.ca/", "CANYON CONSTRUCTION", True),
    ("Hartwell Homes", "https://hartwellhomes.managebuilding.com/Resident/public/home",
     "HARTWELL HOMES", True),
    ("Excel Homes | Calgary Home Builder", "https://excelhomes.ca/",
     "EXCEL HOMES LIMITED PARTNERSHIP", True),
    ("Hamilton Design Studio", "https://hamiltondesign.ca/",
     "HAMILTON DESIGN STUDIO", True),
    # Single-token compound collisions: the domain starts with the brand but its
    # suffix has nothing to do with the rest of the company name.
    ("Meadow Projects", "https://www.meadowprojects.example/",
     "MEADOW DRAFTING AND CONSULTING", False),
    ("Meadow Builder Inc.", "https://meadowbuilder.ca/",
     "MEADOW DRAFTING AND CONSULTING", False),
    # One-word company names must not prefix-match a longer brand.
    ("EYELINE | Stores | Canada", "https://eyeglassshop.example/en/stores/canada/",
     "EYEVA", False),
    # Secondary-token collisions: "crane" and "equipment" both appear in a
    # different company's name, but the primary brand token "forge" does not.
    ("N-Force Crane & Equipment Ltd", "http://www.ironcrestcrane.com/",
     "FORGE CRANE AND EQUIPMENT", False),
    ("Calgary Crane and Rigging", "https://ironcrestcrane.ca/",
     "FORGE CRANE AND EQUIPMENT", False),
    # A genuine Calgary company whose primary token does match must survive.
    ("Arbor Contracting", "https://arborcontracting.ca/", "ARBOR CONTRACTING", True),
    # Geographic/legal noise must not corroborate a match. "CALGARY TECHNOLOGY
    # HOMES" reduced to its one distinctive token "technology", and that word
    # happens to appear in a rental company's title — so "calgary" (a place name)
    # used to supply the second word of corroboration and this matched.
    ("Calgary Tech Rent | Calgary Technology Rentals",
     "https://calgarynorthrent.example/", "CALGARY VERTEX HOMES", False),
    # The same noise rule must not break a real match on the distinctive token.
    ("Technology Homes Inc | Calgary Home Builder",
     "https://vertexhomes.ca/", "CALGARY VERTEX HOMES", True),
    # Third-party directory profiles: the builder's name is in the <title>, so
    # title evidence alone passes "nc in nt" — but the host is unrelated and the
    # URL is a multi-segment profile path (/calgary/builder/<slug>, depth 3).
    # These matched and carried the directory's own email into a sellable list.
    ("Solano Construction | BuilderHub",
     "https://builderhub.example/calgary/builder/solano-construction",
     "SOLANO CONSTRUCTION", False),
    ("Birchglen Group (The) | BuilderHub",
     "https://builderhub.example/calgary/builder/birchglen-group",
     "BIRCHGLEN GROUP (THE)", False),
]

for title, url, company, expected in CASES:
    got = P._name_match(title, url, company)
    check(f"{company[:38]:38} <- {url[:52]:52}", got == expected)

finish()
