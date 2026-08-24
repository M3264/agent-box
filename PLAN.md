# Agent Hub Plan

## 1. Define the smallest useful product

- One workspace
- One job type
- One default team
- Lead conversation as the primary workflow
- Jobs, events, approvals, and artifacts persisted

## 2. Stabilize the backend contract

- SQLite/Postgres schema for jobs, messages, phases, agents, events, approvals, and artifacts
- REST snapshots and cursor-based event history
- WebSocket updates
- Pause, resume, stop, approve, and reject operations
- CLI using the same endpoints

## 3. Build the workflow before styling

- Create job
- Lead receives the user message
- Lead creates a plan
- Specialists execute phases
- Handoffs are explicit records
- Risky actions pause for approval
- Lead produces the final result

## 4. Design the UI around navigation

- Jobs list as the home screen
- Dedicated job detail screen
- Separate tabs or pages for:
  - Conversation
  - Timeline
  - Plan
  - Agents
  - Artifacts
  - Approvals
- Agent inspector opens deliberately instead of occupying the entire screen
- Mobile layout uses stacked views instead of a compressed desktop dashboard

## 5. Implement a thin vertical slice

- Job creation
- Lead conversation
- One specialist phase
- Timeline
- Approval request
- Final result
- Verify the complete workflow before adding more agents or tools

## 6. Add execution capabilities incrementally

- Workspace file tools
- Public research
- Allowlisted commands
- Artifact generation
- Controlled and YOLO modes
- Emergency stop and resource limits

## 7. Add configuration

- Provider profiles
- Team templates
- Server-side secrets
- Practical settings screens and API
- No visual graph editor in v1

## 8. Verify operational behavior

- Browser disconnect and reconnect
- Service restart recovery
- CLI and web state consistency
- Approval persistence
- WebSocket cursor replay
- Error and timeout visibility

## 9. Polish the interface after validation

- Test the workflow with real jobs
- Remove unused panels
- Optimize information density
- Validate desktop and mobile layouts
- Collect feedback before expanding scope
