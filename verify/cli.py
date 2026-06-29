"""CLI tool for verify() Facts library."""

import asyncio
import json
import sys

import aiohttp

from verify import verify, Facts


async def main():
    if len(sys.argv) < 2:
        print("Usage: python -m verify.cli <verification-url>", file=sys.stderr)
        print("Example: python -m verify.cli https://cvm/_api/verification/my-app", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]

    print(f"Verifying: {url}")
    print("-" * 60)

    try:
        async with aiohttp.ClientSession() as session:
            facts = await verify(url, session=session)

        # Pretty print facts
        print(json.dumps(facts.to_dict(), indent=2))

        print("-" * 60)
        if facts.is_valid():
            print("Status: All checks passed (errors list is empty)")
            print("Note: Consumer policy decides accept/reject")
        else:
            print(f"Status: {len(facts.errors)} error(s) detected")
            for err in facts.errors:
                print(f"  - {err}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
