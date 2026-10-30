import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from shipnext.services import CallbackError, apply_callback

logger = logging.getLogger(__name__)


def _body(request):
    """The update as a dict: a JSON object, or form fields; None when it is neither."""
    raw = request.body.decode("utf-8", errors="replace").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(request.POST.items()) or None
    return data if isinstance(data, dict) else None


@csrf_exempt
@require_http_methods(["POST", "PUT", "PATCH"])
def shipnext_callback(request, order_ref):
    """
    Ship Next reports on one order: its tracking number once the parcel is booked, and later
    status changes. The order is named in the URL (``/shipnext/webhook/3001/`` is order #3001).
    """
    data = _body(request)
    if data is None:
        return JsonResponse({"success": False, "message": "The body must be a JSON object."}, status=400)
    try:
        result = apply_callback(order_ref, data)
    except CallbackError as exc:
        logger.warning("Ship Next callback for %s refused: %s", order_ref, exc)
        return JsonResponse({"success": False, "message": str(exc)}, status=exc.status_code)
    return JsonResponse({"success": True, **result})
