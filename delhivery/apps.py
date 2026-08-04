from django.apps import AppConfig


class DelhiveryConfig(AppConfig):
    name = 'delhivery'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from orders.plugins import order_confirmed_registry
        from delhivery.services import trigger_shipment_on_order_confirmed
        
        #register the trigger_shipment function to be called when an order is confirmed
        order_confirmed_registry.register(trigger_shipment_on_order_confirmed)
