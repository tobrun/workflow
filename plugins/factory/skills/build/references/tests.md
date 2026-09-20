# Test Examples

Worked examples of the principles in SKILL.md: behavior over implementation, tests as specification, independent expected values.
See [layers.md](layers.md) for how a scenario's tag decides which layer its test lives at.

## Seams

A **seam** is the public boundary a test exercises, never internals.
Naming the seam before writing the test is what keeps testing effort on real interfaces instead of spreading over every private helper: "what is the public interface here, and which boundary would a caller actually cross?"

The spec's scope section declares the public inputs and outputs of the change, so the seam for a scenario - the boundary its input crosses - is normally a decision you inherit, not one to reopen.
Reopen it only when the declared boundary is wrong or missing: pick the nearest real public boundary, use it, and log the change under Deviations in `implementation-notes.md`.
The signal that a seam is wrong is usually a mocking urge - see [mocking.md](mocking.md)'s "never mock internal collaborators."

## Behavior test vs implementation-coupled test

The seam here is the public `Cart` interface.

Good - exercises the seam, asserts observable behavior:

```ts
test("user can checkout with a valid cart", async () => {
  const cart = new Cart();
  cart.add(product("book", 1200), 2);

  const receipt = await cart.checkout(validPayment());

  expect(receipt.totalCents).toBe(2400);
  expect(receipt.status).toBe("paid");
});
```

Bad - reaches inside and verifies internal wiring:

```ts
test("checkout calls the tax calculator", async () => {
  const taxCalc = jest.spyOn(cart["taxCalculator"], "compute");
  await cart.checkout(validPayment());
  expect(taxCalc).toHaveBeenCalledWith(2400);
});
```

The bad test breaks if tax computation moves, is renamed, or gets inlined, even though checkout behavior is identical.
The good test survives all of those refactors.

## Side-channel verification

Bad - asserts through the database instead of the interface:

```ts
await api.createUser({ name: "Ada" });
const row = await db.query("SELECT * FROM users WHERE name = 'Ada'");
expect(row.status).toBe("active");
```

Good - observes the result the way a caller would:

```ts
await api.createUser({ name: "Ada" });
const user = await api.getUser("Ada");
expect(user.status).toBe("active");
```

If the schema changes but the API contract holds, only the bad test breaks.

## Independent expected values

Bad - tautological, recomputes the expectation the way the code does:

```ts
expect(priceWithVat(100)).toBe(100 * 1.21);
```

Good - the expected value comes from a worked example or the spec:

```ts
// Spec section 4.2: 100.00 EUR at 21% VAT is 121.00 EUR
expect(priceWithVat(100)).toBe(121);
```

If someone changes the VAT logic incorrectly, the tautological test still passes; the literal catches it.

## Test names as specification

Name tests after the capability, not the method under test.

- Good: `"expired coupon is rejected at checkout"`
- Bad: `"test applyCoupon returns false"`

A reader should be able to reconstruct what the system does from the test names alone.
Use the vocabulary the code and existing tests already use, so names match how the team talks about the feature.
