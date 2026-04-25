# RFC 0014: Volume Mount Path Configuration

## Summary
Pass the project's files directory as an environment variable to handlers instead of relying on `import.meta.url` inference.

## Problem
The Deno handler uses `import.meta.url` to find sibling files, but the exact path depends on how the router mounts and imports modules. The router imports from `/daemon-vol/projects/<name>/files/server.ts` so `import.meta.url` resolves to that directory. This works but is fragile — any change to the mount structure breaks handlers.

## Files to Modify
- Deno router — set env var when launching handler modules
- Documentation — update handler template to use the env var

## Implementation
1. When the router imports a project's module, set `process.env.__PROJECT_DIR` (or `Deno.env.set`) to the absolute path of the project's files directory
2. Update the handler template/documentation to use `Deno.env.get("__PROJECT_DIR")` instead of `new URL('.', import.meta.url).pathname`
3. Keep `import.meta.url` as a fallback if the env var isn't set (backward compat)

## Testing & Validation Requirements
- Deploy a handler that reads `Deno.env.get("__PROJECT_DIR")` and serves a file from it. Verify it works.
- Deploy a handler using the old `import.meta.url` approach. Verify it still works (backward compat).
- Change the mount path in the router. Verify the env-var-based handler still works while the import.meta.url one breaks (demonstrating the fix).

## Report Requirements
- Diff of the router code
- Updated handler template with the new approach
- Test showing both old and new approaches work
