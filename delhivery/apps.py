from django.apps import AppConfig


class DelhiveryConfig(AppConfig):
    name = 'delhivery'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from orders.plugins import order_pending_registry
        from delhivery.services import trigger_shipment_on_pending
        
        #register the trigger_shipment function to be called when an order is moved to Pending
        order_pending_registry.register(trigger_shipment_on_pending)
