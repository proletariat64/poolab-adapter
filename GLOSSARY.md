# Poolab Adapter

This context covers the connection between a local Herdr Core and a remote Herdr
Core through an operator's existing poolab access.

## Language

**Adapter**:
The standalone program used as Herdr Core's custom transport. It carries Core
traffic without interpreting the Core protocol.
_Avoid_: Core fork, App plugin

**Carrier**:
The poolab-backed connection used by the adapter to reach the remote command.
It is distinct from the Core protocol carried inside it.
_Avoid_: Core session

**Remote command**:
The complete shell command supplied by local Herdr Core for execution on the
remote machine.
_Avoid_: reconstructed command

**Resource owner**:
The component responsible for the adapter's exclusively created carrier
resources, including cleanup when the adapter exits.
_Avoid_: default Core daemon

**Remote helper**:
The temporary component that connects the carrier to the remote command's
standard streams and enforces carrier lifecycle rules.
_Avoid_: remote Core replacement

**Isolated Core session**:
A Core instance used for acceptance with separate configuration, state, and
connection endpoints, independent from the operator's existing sessions.
_Avoid_: default session, temporary registration
