class Webhooks:
    def __init__(self):
        self.processed = []

    def deliver(self, delivery_id, payload):
        if delivery_id in self.processed:
            return "ignored"
        self.processed.append(delivery_id)
        return "processed"
