"""Person 3: the cloud decision plane (Nimbus PRD v5.0, FR-8 to FR-20).

The core is domain-agnostic. Everything it knows about an application domain
(items, locations, priority classes, rules, task templates, prompts) comes from
a Domain Pack (packs/<name>/), loaded and validated by cloud.pack.
Layers, bottom up: topology/fleet/policy hold the facts the model needs
(context); nlp and tools turn text into a task spec; allocator + checker +
leases decide and commit; orchestrator wires them behind P4's routes.
"""
