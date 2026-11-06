# Code signing policy

## Current status

The project plans to apply for free code signing through SignPath Foundation. It has not yet been approved to use the service, and the current Windows release does not have an Authenticode publisher signature. GitHub build provenance and checksums are separate from an Authenticode signature.

If the project is accepted and begins using the service, the intended attribution is: “Free code signing provided by [SignPath.io](https://signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).” This statement describes the proposed arrangement, not a service currently provided to this project.

## Proposed roles and release process

The following roles are planned for this project if the application is accepted:

- Committer / author: [Ptcr8](https://github.com/Ptcr8).
- Reviewer: [Ptcr8](https://github.com/Ptcr8), including review of contributions from people outside the team.
- Signing approver: [Ptcr8](https://github.com/Ptcr8).

Each release submitted for signing will require manual approval by the signing approver. The team will configure the verified build and signing process after acceptance and follow the applicable [SignPath Foundation conditions](https://signpath.org/terms). Roles and this policy will be updated if the team or process changes.

## Privacy

See [Settings and data handling](_internal/docs/PRIVACY.md) for the application's data storage, network access, tokens, updates, and third-party components.
