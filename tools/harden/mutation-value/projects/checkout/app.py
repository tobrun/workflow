def total(subtotal, member, coupon):
    if subtotal < 0:
        raise ValueError("negative subtotal")
    if coupon not in ("", "SAVE10"):
        raise ValueError("unknown coupon")
    discount = 0
    if member and subtotal >= 100:
        discount = 20
    if coupon == "SAVE10":
        discount += 10
    shipping = 5
    if subtotal >= 100:
        shipping = 0
    return max(0, subtotal - discount) + shipping
