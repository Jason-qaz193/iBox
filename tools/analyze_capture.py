#!/usr/bin/env python3
"""Analyze capture HAR for ibox API calls."""
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
capture = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "package", "求购20260621")

keywords = [
    "advance-orders",
    "order-create",
    "placeOrderMethod",
    "wantToBuy",
    "platform-rate",
    "payment-platforms",
    "consign-orders",
    "market-buy",
]

print(f"Scanning: {capture}\n")

for dp, _, fns in os.walk(capture):
    for fn in fns:
        path = os.path.join(dp, fn)
        if fn.endswith(".har"):
            try:
                data = json.load(open(path, "r", encoding="utf-8"))
            except Exception as e:
                print(f"HAR load error {path}: {e}")
                continue
            entries = data.get("log", {}).get("entries", [])
            print(f"=== HAR {fn} ({len(entries)} entries) ===")
            for e in entries:
                url = e["request"]["url"]
                method = e["request"].get("method", "")
                if not any(k in url for k in keywords):
                    continue
                print(f"\n{method} {url}")
                post = e["request"].get("postData") or {}
                text = post.get("text") or ""
                if text:
                    print(f"  body ({len(text)} chars): {text[:500]}")
                resp = (e.get("response") or {}).get("content") or {}
                rtext = resp.get("text") or ""
                if rtext and len(rtext) < 2000:
                    print(f"  resp: {rtext[:800]}")
                elif rtext:
                    print(f"  resp ({len(rtext)} chars): {rtext[:300]}...")
        elif fn.endswith((".txt", ".json")):
            try:
                content = open(path, "r", encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            if any(k in content for k in keywords):
                print(f"\n--- {path} ---")
                for k in keywords:
                    if k in content:
                        idx = content.find(k)
                        print(f"  [{k}] ...{content[max(0,idx-80):idx+200]}...")

# Extract wantToBuy JS patterns
print("\n\n=== wantToBuy JS analysis ===")
for dp, _, fns in os.walk(capture):
    for fn in fns:
        if not fn.endswith(".har"):
            continue
        har = os.path.join(dp, fn)
        for e in json.load(open(har, "r", encoding="utf-8"))["log"]["entries"]:
            url = e["request"]["url"]
            if "wantToBuy" not in url or not url.endswith(".js"):
                continue
            text = (e["response"].get("content") or {}).get("text") or ""
            print(f"JS from {url[-80:]} size={len(text)}")
            # Find API call patterns
            for pat in [
                r'"/order-create-service/[^"]+"',
                r"'/order-create-service/[^']+'",
                r"advance-orders[^\"']{0,80}",
                r"groupId[^,}]{0,60}",
                r"maxSinglePrice",
                r"quantity",
                r"level",
                r"productType",
            ]:
                hits = re.findall(pat, text)
                if hits:
                    print(f"  {pat}: {sorted(set(hits))[:15]}")
