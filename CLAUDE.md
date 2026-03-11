# Project Goal
An MCP server that uses libvirt-python to provide tools to manage VMs on LibVirt hosts, for example list_vms, create_vm, start_vm, etc.


# Testing
- Robust unit testing should be maintained at all times. There is a test machine available at lionsteel.coalcreek.lan.  Final testing should be tested against this machine.


## Approach

- Basic functionality is already in place, but it requires a thorough review.  The end product should be well tested with integration tests as well as unit tests.
- Maintain a plan that is updated whenever actions are carried out. The plan should include multiple steps for each broad goal, with testable success criteria at each step.

## Coding standards

- Use latest versions of libraries and idiomatic approaches as of today.
- Keep it simple - NEVER over-engineer, ALWAYS simplify, NO unnecessary defensive programming. No extra features - focus on simplicity.
- Be concise. Keep README minimal. IMPORTANT: no emojis ever.
- When hitting issues, always identify root cause before trying a fix. Do not guess. Prove with evidence, then fix the root cause.

## Working documentation

- All documents for planning and executing this project will be in the docs/ directory.
