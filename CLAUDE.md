# CLAUDE.md

Engineering rules for this repository live in `AGENTS.md` and apply here in full.

- **This is a library.** Every change ships through a tagged release, and both applications pin
  it. Prefer additive changes. Flag any change that would force a major release before making
  it.
- **Refuse business meaning.** If a request would add a role, prompt, schema, API model or
  workflow here, say it belongs in `requirement-portal` or `knowledge-portal`, and do not add
  it.
- **Run the gates in `AGENTS.md` §6 after each change.**
