class Webhooks:
    def __init__(self):
        self.processed = []

    def deliver(self, delivery_id, payload):
        self.processed.append(delivery_id)
        return "processed"
