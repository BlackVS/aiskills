# Prompt: make this repository more agent-friendly

Use with the `oh-improve-agent-readiness` skill loaded. Needs an existing
readiness report — run the `agent-readiness` prompt first if there is none.

---

Using the agent readiness report [PATH TO REPORT / "from the previous run"],
propose the 5–10 highest-impact fixes for [PATH TO REPO], ranked as the skill
describes, with the concrete file each fix touches and what an agent cannot do
today without it. Wait for my approval before implementing; then apply the
approved fixes as atomic commits and update the report's checkmarks and counts.
