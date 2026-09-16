class Webhooks:
    def __init__(self):
        self.processed = []
        self.ignored = 0

    def deliver(self, delivery_id, payload):
        if delivery_id in self.processed:
            self.ignored += 1
            return "ignored"
        self.processed.append(delivery_id)
        return "processed"

    def stats(self):
        return {"processed": len(self.processed), "ignored": self.ignored}
