"""Simulated fleet so the dashboard shows what 1,200 wearables look like.

Simulated workers run the exact same heat engine as the real wearable; only
their motion and incidents are synthetic. Every simulated alert is flagged
`simulated: true` so nobody mistakes it for real data.
"""
import random

FIRST = ["Ravi", "Imran", "Suresh", "Abdul", "Mohammed", "Rajesh", "Anil", "Tariq", "Bilal",
         "Sanjay", "Joseph", "Samuel", "Kiran", "Faisal", "Nasir", "Ramesh", "Hari", "Arjun",
         "Deepak", "Yusuf", "Sunil", "Asif", "Prakash", "Manoj", "Kwame", "Emmanuel", "Omar",
         "Hassan", "Ali", "Vinod", "Shahid", "Rafiq", "Babu", "Gopal", "Naveen", "Salim"]
LAST = ["Kumar", "Khan", "Singh", "Rahman", "Hussain", "Nair", "Das", "Yadav", "Ali",
        "Shaikh", "Thapa", "Gurung", "Mensah", "Okafor", "Pillai", "Reddy", "Iqbal", "Ahmed",
        "Chowdhury", "Mondal", "Rana", "Tamang", "Bose", "Menon", "Qureshi", "Butt"]
TRADES = [("Steel fixer", "heavy"), ("Mason", "moderate"), ("Carpenter", "moderate"),
          ("Scaffolder", "heavy"), ("Electrician", "light"), ("Plumber", "moderate"),
          ("Welder", "moderate"), ("General labourer", "heavy"), ("Painter", "light"),
          ("Crane signaller", "light"), ("Surveyor", "light"), ("Tile fixer", "moderate")]
ZONES = ["Tower A", "Tower B", "Podium", "Basement", "Road works", "Façade", "Laydown yard"]
# Share of full sun each zone gets; the basement is shaded, the podium partly covered.
ZONE_SUN = {"Tower A": 1.0, "Tower B": 1.0, "Podium": 0.5, "Basement": 0.0,
            "Road works": 1.0, "Façade": 0.8, "Laydown yard": 1.0}
# Zone centres as offsets from the site pin (degrees). The StickS3 has no GPS, so a
# worker's location is their zone's centre; a GPS unit or gateway RSSI would refine it.
ZONE_OFFSET = {"Tower A": (0.0012, -0.0010), "Tower B": (0.0012, 0.0008), "Podium": (0.0, 0.0),
               "Basement": (-0.0002, 0.0002), "Road works": (-0.0020, 0.0015), "Façade": (0.0010, 0.0),
               "Laydown yard": (-0.0015, -0.0018)}

INCIDENTS = [
    # type, severity, weight, title, message
    ("rest_violation", "warning", 30, "Working during cool-down",
     "Motion shows moderate work during a mandatory cool-down."),
    ("unwell", "warning", 16, "Worker reports feeling unwell",
     "Held the report button. Possible early heat exhaustion."),
    ("inactivity", "warning", 14, "No movement for 5 min",
     "Worker did not move during a work cycle. Check-in prompt sent."),
    ("fall", "critical", 12, "Fall detected",
     "Free-fall then impact, no movement afterwards. Waiting for response."),
    ("heat_critical", "critical", 10, "Heat illness risk",
     "Shift exposure well above limit with rising activity. Move to shade now."),
    ("tremor", "warning", 8, "Unusual shaking",
     "Rhythmic 5–7 Hz shaking for more than 4 s."),
    ("sos", "critical", 6, "SOS pressed", "Worker held the SOS button."),
    ("offline", "info", 4, "Wearable offline", "No data for 60 s. Battery or coverage."),
]


def make_fleet(n, seed=7):
    rng = random.Random(seed)
    fleet = []
    for i in range(n):
        trade, base = rng.choice(TRADES)
        wl = base if rng.random() < 0.7 else rng.choice(("light", "moderate", "heavy"))
        fleet.append({
            "id": "W-%04d" % (i + 1),
            "name": "%s %s" % (rng.choice(FIRST), rng.choice(LAST)),
            "trade": trade,
            "zone": rng.choice(ZONES),
            "crew": "%s-%d" % ("ABCDEFG"[i % 7], 1 + (i // 7) % 9),
            "live": False,
            "acclimatized": rng.random() > 0.12,
            "workload": wl,
            "phase": "work",
            "phase_reason": "",
            # stagger everyone across their cycle so rests don't all line up
            "work_elapsed_min": rng.random() * 40,
            "work_budget_min": 45,
            "rest_remaining_min": 0.0,
            "rest_required_min": 15,
            "shift_exposure_min": 60 + rng.random() * 240,
            "device_temp_c": round(36 + rng.random() * 8, 1),
            "battery": rng.randint(35, 100),
            "last_event": None,
            "open_alerts": 0,
            "offline": rng.random() < 0.006,
            "updated": 0.0,
        })
    return fleet


def pick_incident(rng, level):
    pool = [x for x in INCIDENTS if x[0] != "heat_critical" or level >= 2]
    total = sum(x[2] for x in pool)
    r = rng.random() * total
    for x in pool:
        r -= x[2]
        if r <= 0:
            return x
    return pool[0]
