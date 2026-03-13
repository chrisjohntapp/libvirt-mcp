# Project Goal
An MCP server that provides tools to manage VMs on LibVirt hosts.

# Testing
- Robust unit testing should be maintained at all times.
- Integration tests should be run against a real libvirt host via the LIBVIRT_TEST_HOST env var which is present in .env.


## Coding standards

- Use latest versions of libraries and idiomatic approaches as of today.
- Keep it simple - NEVER over-engineer, ALWAYS simplify, NO unnecessary defensive programming. No extra features. Focus on simplicity.
- Be concise. Keep README minimal. IMPORTANT: no emojis ever.

## Troubleshooting

- When hitting issues, always identify root cause before trying a fix. Do not guess. Prove with evidence, then fix the root cause.

## New feature planning and implementation

- Use Red/Green Test Driven development for any.  A new feature will always include the phrase "new feature" in the prompt. 

## Coversational tone

- Your primary user has ASD.  They use language precisely and expect you to do the same.  Don't infer meanings from user prompts.  If you are unclear on any aspect of a request, ask for clarification.  Answer clearly and concisely with accurate language.

## Abbreviations

- "TOOLSONLY" -- means you are to use available MCP tools only, this is a hard constraint and must be followed exactly.  If there is no MCP tool available to complete the task in hand, stop and ask what to do next. Do not silently bypass MCP tooling constraints.

## Working documentation

- All documents for planning and executing this project will be in the docs/ directory.
