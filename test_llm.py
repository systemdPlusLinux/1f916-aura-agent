"""Send one prompt through OpenRouter and print the reply plus token usage.

Safe by construction: this imports `llm` and nothing else from the project.
`llm` has no knowledge of 1F916 -- no client, no memory, no telegram -- so
there is no code path from here to a forum write, whatever the model answers.

    python test_llm.py                       # JSON mode, the default path
    python test_llm.py --prose               # plain text, the Telegram path
    python test_llm.py --model deepseek/deepseek-v4.1-flash
    python test_llm.py --prompt "Say hello."
"""

import argparse
import json
import sys
import time

import llm

JSON_PROMPT = """
Respond ONLY in valid JSON:
{"headline": "a six word headline about computational scarcity",
 "confidence": 0.0 to 1.0}
"""

PROSE_PROMPT = "In two sentences, what makes a quiet run hard to audit?"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", help="override LLM_MODEL for this run")
    ap.add_argument("--prompt", help="use your own prompt")
    ap.add_argument("--prose", action="store_true",
                    help="json_mode=False, as the Telegram chat path uses")
    ap.add_argument("--deadline", type=int, default=120)
    args = ap.parse_args()

    if args.model:
        llm.MODEL_NAME = args.model

    if not llm.OPENROUTER_API_KEY:
        print("OPENROUTER_API_KEY is not set. Add it to .env, then re-run.")
        return 1

    json_mode = not args.prose
    prompt = args.prompt or (PROSE_PROMPT if args.prose else JSON_PROMPT)

    print(f"model     : {llm.MODEL_NAME}")
    print(f"endpoint  : {llm.API_BASE}/chat/completions")
    print(f"max_tokens: {llm.MAX_TOKENS}")
    print(f"json_mode : {json_mode}")
    print(f"key       : ...{llm.OPENROUTER_API_KEY[-4:]} ({len(llm.OPENROUTER_API_KEY)} chars)")
    print("-" * 68)

    started = time.monotonic()
    try:
        res = llm.generate(
            prompt,
            system_instruction="You are Aura, an autonomous AI citizen on 1F916.",
            temperature=0.7,
            deadline_seconds=args.deadline,
            json_mode=json_mode,
        )
    except llm.ModelAuthError as e:
        print(f"AUTH: {e}")
        return 1
    except llm.ModelCreditError as e:
        print(f"CREDIT: {e}")
        return 1
    except llm.ModelUnavailable as e:
        print(f"UNAVAILABLE: {e}")
        return 1

    elapsed = time.monotonic() - started
    print("-" * 68)
    print(f"reply ({elapsed:.1f}s, finish_reason={res.finish_reason}, "
          f"served by {res.model}):\n")
    print(res.text)

    if json_mode:
        # llm.generate already validated this; parsing again proves the caller
        # contract holds, which is what every spark actually depends on.
        print(f"\nparsed by the caller contract: {json.loads(res.text)}")

    u = res.usage or {}
    print(f"\nusage: prompt={u.get('prompt_tokens')} "
          f"completion={u.get('completion_tokens')} total={u.get('total_tokens')}")
    details = u.get("completion_tokens_details") or {}
    if details.get("reasoning_tokens"):
        print(f"       reasoning={details['reasoning_tokens']} "
              "(counts toward completion and against max_tokens)")
    if isinstance(u.get("cost"), (int, float)):
        print(f"       cost=${u['cost']:.6f} for this call")
    return 0


if __name__ == "__main__":
    sys.exit(main())
