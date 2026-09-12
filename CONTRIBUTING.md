# Contributing Guide

## Commit Messages

Good commit messages make the project history readable, simplify debugging, and make it easier to understand why a change was made — not just what changed.

### The Core Idea

Write your subject line as if completing this sentence:

> "If applied, this commit will…"

For example: *"If applied, this commit will **add justfile for CWL dev/test/prod execution**"*

---

### Format

```
<type>(<scope>): <short summary>

<optional body — explain what and why, not how>

<optional footer — issue references, breaking changes>
```

- Subject line: 50 characters or less, no trailing period
- Body lines: wrap at 72 characters
- Use **imperative mood** — `Add`, `Fix`, `Remove`, not `Added`, `Fixed`, `Removed`

---

### Commit Types

| Type | Use for |
|---|---|
| `feat` | Adding something new that didn't exist before |
| `fix` | Correcting something that was broken |
| `refactor` | Restructuring existing code without changing behavior |
| `docs` | Documentation only |
| `style` | Formatting, whitespace (no logic change) |
| `test` | Adding or fixing tests |
| `chore` | Maintenance — updating dependencies, `.gitignore`, etc. |
| `perf` | Performance improvement |

---

### Choosing the Right Type

**`feat`** — something new that someone can now do:
```
feat(workflow): add justfile for CWL dev/test/prod execution
```

**`refactor`** — same behavior, better structure (e.g., applying DRY):
```
refactor(config): centralize shared value into config file
```
> If a value is repeated across multiple scripts, moving it to a single config file and importing it elsewhere is a refactor — behavior is unchanged, but there is now one source of truth.

**`fix`** — something was wrong:
```
fix(api): handle null response from payment gateway
```

**`chore`** — housekeeping with no functional impact:
```
chore(deps): bump lodash from 4.17.20 to 4.17.21
```

---

### Writing a Good Body

Use the body to explain **what and why**, not how. Example:

```
fix(cart): prevent duplicate items on rapid clicks

Debounce was missing on the add-to-cart button, causing
multiple POST requests when users clicked quickly. Added
a 300ms debounce and an optimistic UI lock.

Closes #412
```

---

### Footer Conventions

```
Closes #123        — auto-closes a GitHub/GitLab issue
Refs #456          — references without closing
BREAKING CHANGE:   — signals a major API change
```

---

### One Change Per Commit

If your subject line needs the word "and", consider splitting into two commits. Each commit should represent one logical change — this keeps history clean and makes individual changes revertable.

If you need to stage only part of your current changes:
```bash
git add -p
```
This opens an interactive mode where you can select which parts of the diff to include in the current commit.

---

### Quick Checklist

- [ ] Subject line under 50 characters?
- [ ] Imperative mood (`Add` not `Added`)?
- [ ] Correct type chosen?
- [ ] Does the body explain *why* if the change needs context?
- [ ] Is this one logical change, not several bundled together?
- [ ] Issue referenced in footer if applicable?
