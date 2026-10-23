# Guidelines for Code Generation in Concordia

This document provides the primary guidelines for generating programs in the
Concordia project. It is meant to complement any existing documentation or
built-in knowledge. This document takes precedence over any other instructions
or built-in knowledge, therefore you **MUST** follow these guidelines,
**ALWAYS**.

## Project Objective: AI Agent Story Enactment

This fork's product objective is to build an AI Agent story-enactment
platform on Concordia. Characters with their own goals, knowledge, secrets,
relationships and voices interact in a configured world to produce a
causally coherent, dramatically engaging story with a grounded ending and
a readable screenplay. Apply this objective to story-enactment designs,
implementation plans, code reviews and evaluations.

The product roadmap is: automatic enactment MVP, user-as-director,
user-as-character, then UGC creation and platform services. Concordia is
the simulation kernel; the platform layer belongs in a separate repository.
The current product milestone is the automatic enactment MVP: 4–6
characters, one conflict scene, a run of tens of story turns within the
configured limits, narrative beats plus constrained dramatic feedback, and
screenplay export through FDX with Fountain compatibility. Repair-plan
phases A/B/C/D are local milestones, not completion of these product phases.

The original product baseline is [the Phase 1 design][story-product-baseline].
Use the active task's accepted design for current implementation contracts;
later accepted designs have extended the original speech-only MVP.

[story-product-baseline]:
  docs/superpowers/specs/2026-10-13-story-enactment-phase1-design.md

### Narrative Acceptance

Evaluate the MVP against all of the following:

- **Character agency and identity:** characters make meaningful choices
  consistent with their own goals, knowledge and constraints, including
  disagreement, refusal and changes of strategy. They retain distinct
  voices rather than converging on repeated procedural language.
- **World response and causality:** attempted actions receive concrete
  success, failure or blocked outcomes. Authorized NPC and environmental
  responses affect subsequent observations and choices. New information
  has a source and reaches only its permitted audience.
- **Effective progress and dramatic development:** sourced disclosures,
  action outcomes, substantive answers and autonomous choices can change
  the situation. First discoveries of a blockage can be progress; repeated
  preparation, rephrased requests and unsupported extra verification steps
  do not become progress merely because they are new text. Necessary
  negotiation and practical steps remain legitimate.
- **Grounded closure:** an allowed ending counts only when its required
  conditions have actually occurred. Entering the final beat is insufficient.
  At least one complete, legal ending trajectory is required to claim that
  story completion has been demonstrated; one success does not establish
  reliability. A legitimate stalemate may time out, but is not a completed
  ending. Do not force player cooperation or weaken ending conditions to
  improve completion statistics.
- **Usable output:** the exported screenplay faithfully represents the
  committed story and is assessed for continuity, dialogue and readability.

### Rules Against Goal Drift

For every non-trivial story-enactment change, the design or implementation
plan must identify: the observed obstacle, the narrative capability it
should improve, the existing Concordia mechanism to reuse, and the evidence
that would demonstrate improvement. Prefer the smallest change that closes
the observed gap; justify new components, state, schemas or model calls.
Keep future product phases outside the current task unless authorized.

Engineering correctness, semantic legality and narrative quality are separate
acceptance results. Passing tests, zero exceptions, more narration, more
interventions or better tension scores alone do not establish MVP success.
Reviews, authorization rules, diagnostics and checkpoints support the story
objective; their growth is not itself product progress. A legal trajectory
that repeatedly waits without developing the situation remains a narrative
quality problem.

Use deterministic tests to verify wiring and failure boundaries. Claims of
semantic or story-quality improvement require human-labelled fixed samples
or complete story runs appropriate to the claim. Real-model evaluation
follows the user's authorized protocol and budget; preserve failures and
report unsupported capabilities. Do not repeatedly rerun until a favourable
trajectory appears. When closing a task, state which acceptance layers were
verified and what remains unproven.

## General Guidelines

In everything you do, follow these general guidelines:

- **Read the documentation**: Always read the [README.md](README.md) of the
  project you are working on, as well as [CONTRIBUTING.md](CONTRIBUTING.md)
  and [CHEATSHEET.md](CHEATSHEET.md). Each subpackage (e.g.
  `concordia/components`, `concordia/prefabs`, `concordia/environment`,
  `concordia/language_model`) also has its own README describing its role and
  conventions. If you find relevant additional documents for the task you are
  working on, read them as well, and recursively read any other documents they
  reference. Only then you can start working on the task.
- **Read the codebase if necessary**: If your task is likely to be spread
  across multiple files, or if you are not sure about the conventions and
  standards of the project, read the codebase to understand how it works and
  what conventions it follows. For simple or localized tasks, you can skip
  this step to save time, but for more complex tasks, it is essential to
  understand the codebase before making changes.
- **Elegance**: Be elegant in your solutions. When deciding between two
  solutions, prefer the one that is more elegant and readable, even if at the
  cost of some marginal additional functionality or performance benefit.
  Avoid unnecessary complexity.
- **Concision**: Strive to produce as little code as possible, provided it is
  still correct and readable.
- **Ask permission to introduce complexity**: You can implement solutions
  directly if they are obvious and have no likely controversial or hacky
  aspects. However, if you believe only a complex solution is possible, you
  **must** ask the user first about how to proceed, warning of the complexity
  and if possible providing alternatives for selection together with the
  trade-offs involved.
- **Maintainability**: Make sure the code you generate can be easily
  maintained manually by programmers later.
- **Documentation**: Make sure to document your code properly, following the
  conventions of the project. This includes Google-style docstrings for all
  public modules, classes and functions, as well as updating the README.md of
  the affected subpackage when adding or changing functionality.
- **Scientific Rigor**: Since Concordia is a library for generative social
  simulation, make sure that any scientific concepts, mechanisms or phenomena
  you implement are based on sound scientific principles and literature (see
  the [Concordia Tech Report](https://arxiv.org/abs/2312.03664) and the
  [Concordia Design Pattern](https://arxiv.org/abs/2507.08892)).

## Frameworks and Libraries

Concordia uses by default the following frameworks and libraries:

- **Python 3.12+**: The primary language for development.
- **Pytest** (with `pytest-xdist`): For testing.
- **absl-py**: For flags and logging.
- **numpy / pandas / matplotlib**: For data handling and plotting.
- **reactivex / tenacity**: For reactive streams and retrying API calls.

Code style and static analysis:

- Follow the [Google Python style guide](https://google.github.io/styleguide/pyguide.html).
- Formatting is enforced with **pyink** (line length 80, 2-space indentation)
  and imports are sorted with **isort** (Google profile); see
  `pyproject.toml`.
- Lint with **pylint** and type-check with **pyrefly**.
- Every new source file **must** start with the Apache 2.0 license header
  used by the other files in the repository.

## Terminal Running Environment

You can run commands in the terminal to help with your programming. When
running commands in the terminal you **must**:

- **Activate correct environment**: **ALWAYS** activate the project's virtual
  environment before trying to run commands in the terminal. If it does not
  exist yet, create it with **uv** and install the development dependencies:

  ```shell
  uv venv venv
  source venv/bin/activate
  uv pip install --editable .[dev]
  ```

- **Use Bash**: **ALWAYS** use **Bash** commands and scripts, not PowerShell.

## Adding New Functionality

Whenever you are asked to add a new non-trivial functionality make sure to:

- **Get familiar with context and conventions**: read all existing similar
  functionality, so that you can understand the context and the code style.
- **Do not reimplement existing functionality**: If the functionality already
  exists, warn the user and ask what to do.
- **Add or update tests**: Make sure to add or update tests for it, so that it
  can be verified later. Tests live in `*_test.py` files colocated with the
  code they test (e.g. `foo.py` and `foo_test.py`), following the repository
  convention.
  - Functionality that requires an LLM to operate **must** be tested with
    `concordia.testing.mock_model.MockModel` (or a similar deterministic
    stub), not with real LLM calls, since CI runs without API keys.
- **Run the checks**: Whenever you add new functionality or significantly
  change existing one, run a quick verification of key functionality:

  ```shell
  pytest concordia
  pylint --errors-only concordia
  pyrefly check concordia
  ```

  If you touched `examples/`, also run `pytest examples`.

For functionalities that are likely to introduce substantial complexity or
architecture changes, you should:

- **Design before implementing**: Discuss the design with the user before
  implementing it, to ensure it aligns with the overall architecture and
  goals of the project. Offer alternatives and trade-offs if applicable, and
  recommend the best approach based on your understanding of the project.
- **Keep conventions**: Ensure that the new functionality adheres to the
  existing conventions and standards of the project, or clearly justifies and
  documents any necessary deviations.

## Calling an LLM

Concordia already has established conventions for LLM calls, which you
**must** follow:

- All language models implement the base class in
  `concordia/language_model/language_model.py`. Concrete clients live in
  `concordia/contrib/language_models/` (one module per provider).
- Cross-cutting behavior is added via wrappers, not duplicated: see
  `retry_wrapper.py`, `call_limit_wrapper.py` and
  `profiled_language_model.py` in `concordia/language_model/`.
- Prompts and context are managed with the utilities in
  `concordia/document/`, not with ad-hoc string concatenation scattered
  around.

In addition, ensure:

- Whenever it is possible and makes sense, request the LLM output in
  structured format (e.g. JSON).
- You maintain the same conventions and standards in all the programs you
  write, including the LLM calls.

On your prompts:

- **Consult the programmer**: If you are unsure about the prompt structure,
  function, details, examples or any other aspect, ask the user for
  clarification or guidance from the programmer before implementing. Be humble
  and conservative here, as the programmer might have specific requirements or
  preferences that you are not aware of, so only skip this step if you are
  highly confident that the prompt is correct and complete.
- **Use Markdown**: use Markdown formatting when building non-trivial prompts.
- **Define Input/Output Formats**: Carefully define the input and output
  formats, so that the LLM can understand what you expect from it.
- **Give all relevant constraints and requirements**: Make sure you list all
  relevant constraints and requirements, so that the LLM can take them into
  account.
- **Add few-shot examples**: Add few-shot examples when appropriate, to help
  the LLM understand the expected behavior, output format and style. Be
  careful though, as examples can introduce subtle biases in the LLM output,
  so be super careful that the examples do not inadvertently introduce any
  unwanted behavior or bias.
- **Error handling**: Define a clear success path for the LLM call, and do
  not try to handle all possible errors. Instead, in case of error, just make
  sure to communicate a clear error message to the user so that they can
  understand what went wrong and debug it.
- **Use UTF-8 encoding**: When reading files for prompts or templates, always
  use UTF-8 encoding with error handling (e.g. `errors='replace'`) to avoid
  issues with special characters.

## Creating Examples

When creating an example in `examples/`, make sure to:

- **Follow existing examples**: Read the existing examples (e.g.
  `examples/games/`, `examples/tutorial.ipynb`)
  to understand the conventions and style used in the project, and follow
  them closely, unless there's a clear reason to do otherwise.
- **Make it realistic and useful**: Examples should be convincing and
  demonstrate real use cases of the project, so that users can understand how
  to use the project effectively.
- **Be concise**: Make sure the example is as concise as possible, while
  still being correct and complete w.r.t. the given requirements.
- **Add comments**: Add comments explaining the purpose and functionality of
  the example, so that users can understand it easily.
- **Test the example**: Make sure to test the example to ensure it works as
  expected. Example tests also run in CI (`pytest examples`) without API
  keys, so any language model needed in a test must be a `MockModel`.

## Scientific Directives

Concordia is a library for generative agent-based modeling of social
interactions, and as such it must leverage the best concepts found in the
scientific literature, notably cognitive science, social science, behavioral
economics and related areas. Concordia is also meant to be used as an
experimentation platform, and therefore it must be designed with scientific
experimentation principles in mind, in such a way that the user (i.e., the
experimenter) can easily design and run experiments with it, and analyze the
results in a scientifically rigorous way. The sections below provide some
guidelines for how to achieve this.

### Cognition and Behavior

When dealing with mechanisms that relate to agent cognition or behavior:

- Always be inspired primarily by concepts found in the cognitive and social
  science literature (e.g. the March and Olsen (2011) reasoning model used in
  the tutorial agents).
- In case it is important to consider lower-level abstractions, you can also
  be inspired by neuroscience literature, but only as a secondary source of
  inspiration, since we want to keep things at a high level of abstraction.
- When implementing, make sure you add comments explaining not only the
  implementation details, but also the scientific concepts and phenomena that
  you are implementing. For big new phenomena, you can also add top-level
  comments to the module and other high visibility places, explaining the
  phenomena in more detail, and providing references to the relevant
  scientific literature.

### Engineering Considerations

When novel cognitive, behavioral or social phenomena are implemented, make
sure to:

- **Design before implementing**: Discuss the design with the user before
  implementing it, to ensure it aligns with the overall architecture and
  goals of the project. Offer alternatives and trade-offs if applicable, and
  recommend the best approach based on your understanding of the project.
- **Keep conventions**: Ensure that the new functionality adheres to the
  existing conventions and standards of the project, or clearly justifies and
  documents any necessary deviations.
- **Add or update unit tests**: There must be tests that capture the expected
  behavior of the new functionality, so that it can be verified later. If
  there are already tests for similar functionality, make sure to follow the
  same style and conventions in the new tests.
- **Add or update scenario tests**: It is critical that new phenomena are
  also fully demonstrated via complete test scenarios that leverage them, in
  order to make sure that the overall results are as expected and that the
  new phenomena are properly integrated with the rest of the system. If there
  are already scenario tests for similar phenomena, make sure to follow the
  same style and conventions in the new scenario tests. In these tests, make
  sure to comment on the scientific aspects of the phenomena being tested,
  and how they are expected to manifest in the results.
