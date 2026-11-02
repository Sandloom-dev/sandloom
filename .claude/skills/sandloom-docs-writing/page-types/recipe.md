# Cookbook recipe

`type: recipe` · Reader: has a business problem and knows the concepts · Level: `building`

For a reader with a business problem who knows the concepts. A how-to does one framework operation; a recipe combines several. Each recipe answers one job. Recipes are independent of each other, and the index groups them by problem domain with one line each (R9).

1. **Title (R1).** The reader's task in their words: "Stop a duplicate webhook from charging twice", not "Using `@on` with durable inbox".
2. **Problem (R2).** One or two sentences with a concrete situation.
3. **Solution (R3).** One to four sentences naming the mechanisms, each linked to its concept. The concept page does the explaining.
4. **Code (R4).** One complete, tested module path from an example app, included by marker, imports shown, nothing elided.
5. **Run and verify (R5).** A test or command with literal expected output.
6. **When to use, trade-offs, pitfalls (R6).** One or two pitfalls with the `SANDLOOM-*` code that fires.
7. **Variation (R7).** Monolith versus split, only if behaviour differs.
8. **Related (R8).** At most 3–5 links.

Model: [Temporal Polling](https://docs.temporal.io/design-patterns/polling).
