"""Use cases: the application business rules.

Interactors orchestrate the entities to do what the app does (analyse a race, ...). They
depend only on the entities and on the interfaces declared here (gateways, request and
response models), which the outer circles implement (the Clean Architecture refactor, #6).
"""
