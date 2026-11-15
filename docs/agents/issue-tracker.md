# Issue tracker: GitHub

Issues and specifications live in proletariat64/poolab-adapter GitHub Issues.
Use the gh CLI. Infer the repository from origin.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels`
- Comment: `gh issue comment <number> --body-file <file>`
- Label: `gh issue edit <number> --add-label "..." / --remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

Use conventional prefixes in issue and PR titles. Include observable acceptance
criteria in specifications. Exclude private environment data from public issues.

## Pull requests as a triage surface

PRs as a request surface: no.

## Skill operations

"Publish to the issue tracker" means create a GitHub issue.
"Fetch the relevant ticket" means read the issue and its comments.
