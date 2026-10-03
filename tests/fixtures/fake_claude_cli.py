"""Fake ``claude`` CLI for offline llm_note tests (no network, no model).

Behaviour is chosen by the ``FAKE_CLAUDE_MODE`` environment variable. It
reads the packet from stdin (header line + JSON) and, in the valid modes,
answers with a note built only from numbers/paths that exist in the packet.
``FAKE_CLAUDE_PROBE`` (a file path) receives what the child saw: argv, the
working directory and whether API-key variables were present.
"""

import json
import os
import sys
import time

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
probe = os.environ.get("FAKE_CLAUDE_PROBE")
if probe:
    with open(probe, "w", encoding="utf-8") as handle:
        json.dump({
            "argv": sys.argv[1:], "cwd": os.getcwd(),
            "has_api_key": "ANTHROPIC_API_KEY" in os.environ,
            "has_auth_token": "ANTHROPIC_AUTH_TOKEN" in os.environ,
        }, handle)

text = sys.stdin.read()
packet = json.loads(text.split("\n", 1)[1])


def valid_note() -> dict:
    assessments = []
    for item in packet["required_assessments"]:
        block = packet["symbols"][item["symbol"]]
        day = sorted(block["days"])[-1]
        code = sorted(block["days"][day]["akd"])[0]
        net = block["days"][day]["akd"][code][0]
        assessments.append({
            "symbol": item["symbol"], "rule_id": item["rule_id"], "as_of": item["as_of"],
            "assessment": "destekliyor",
            "reasons": [{
                "text": f"{day} tarihinde {code} kodunun net_size değeri {net}; hipotezle tutarlı.",
                "evidence_paths": [f"/symbols/{item['symbol']}/days/{day}/akd/{code}"],
            }],
        })
    return {"assessments": assessments, "data_caveats": ["Takas verisi gecikmeli olabilir."]}


def envelope(note) -> str:
    return json.dumps({
        "type": "result", "is_error": False, "result": "ok",
        "structured_output": note, "modelUsage": {"claude-opus-5-5": {"inputTokens": 1}},
        "usage": {"input_tokens": 11, "output_tokens": 22, "service_tier": "standard"},
        "duration_ms": 1234, "num_turns": 1, "total_cost_usd": 0.5,
    }, ensure_ascii=False)


note = valid_note()
# FAKE_CLAUDE_ONLY_SYMBOL: the mode applies to that symbol's call only; every
# other symbol gets a valid answer. FAKE_CLAUDE_LOG: one line per call.
only = os.environ.get("FAKE_CLAUDE_ONLY_SYMBOL")
if only and packet["required_assessments"][0]["symbol"] != only:
    mode = "ok"
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(packet["required_assessments"][0]["symbol"] + " " + mode + "\n")
if mode == "timeout":
    time.sleep(30)
elif mode == "refusal":
    print(json.dumps({"type": "result", "is_error": True, "stop_reason": "refusal", "result": "no"}))
    sys.exit(0)
elif mode == "limit":
    print("Claude usage limit reached. Your limit will reset at 5pm.", file=sys.stderr)
    sys.exit(1)
elif mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(2)
elif mode == "nonjson":
    print("this is not json")
    sys.exit(0)
elif mode == "result_text":
    print(json.dumps({"type": "result", "is_error": False, "result": "```json\n" + json.dumps(note) + "\n```"}))
    sys.exit(0)
elif mode == "result_garbage":
    print(json.dumps({"type": "result", "is_error": False, "result": "Merhaba, işte not"}))
    sys.exit(0)
elif mode == "schema_bad":
    note["extra"] = 1
elif mode == "bad_enum":
    note["assessments"][0]["assessment"] = "güçlü"
elif mode == "forbidden":
    note["assessments"][0]["reasons"][0]["text"] += " Hedef fiyat yükselir."
elif mode == "forbidden_buy":
    note["assessments"][0]["reasons"][0]["text"] = "Bu hissede al yönlü bir görünüm var."
elif mode == "bad_number":
    note["assessments"][0]["reasons"][0]["text"] += " Toplam 987654321 lot."
elif mode == "bad_path":
    note["assessments"][0]["reasons"][0]["evidence_paths"] = ["/symbols/NOPE/days"]
elif mode == "missing":
    note["assessments"] = note["assessments"][:-1]
elif mode == "secret":
    note["assessments"][0]["reasons"][0]["text"] += " bkz https://example.invalid/x"
print(envelope(note))
