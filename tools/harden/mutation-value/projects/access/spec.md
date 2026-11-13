Inputs are a role string, boolean ownership and lock flags, and integer
timestamps. Locked resources deny everyone. Expiry is exclusive: deny when
expires is less than or equal to now. Before expiry, an admin can edit
regardless of ownership, and an editor can edit only if they own the resource.
All other roles deny. Lock and expiry checks take precedence over role.
