from django.shortcuts import render_to_response
from django.template.loader import render_to_string
from django.template import RequestContext
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from reg.models import *
from reg.forms import *

from django.forms.util import ErrorList

from django.http import Http404, HttpResponse

from sked.urls import CURRENT_EVENT

import json
from collections import defaultdict

import braintree

def register(request):
    payment_form = PaymentForm()
    ticket_form = TicketForm()

    ticket_types = TicketType.objects.filter(event=CURRENT_EVENT, enabled=True).order_by('position')

    attendees = Ticket.objects.filter(event=CURRENT_EVENT, success=True).order_by('-id')

    return render_to_response('reg/register.html', {'ticket_form': ticket_form, 'payment_form': payment_form, 'ticket_types': ticket_types, 'attendees': attendees, 'BRAINTREE_CSE_KEY': getattr(settings, "BRAINTREE_CSE_KEY", "")}, context_instance=RequestContext(request))

@csrf_exempt
def save(request):
    if request.method != "POST":
        raise Http404
    try:
        data = json.loads(request.raw_post_data)
    except:
        raise Http404

    out = {'ticket_forms': [], 'success': True}
    types = defaultdict(int)
    valid_tickets = []
    for form_id, form_data in data['ticket_forms'].items():
        form_response = {'id': form_id}
        form = TicketForm(form_data)
        if form.is_valid():
            form_response['success'] = True
            valid_tickets.append(form)
            types[int(form_data['type'])] += 1
        else:
            out['success'] = False
            form_response['success'] = False
            form_response['text'] = render_to_string('reg/partials/ticket_form.html', {'ticket_form': form}, context_instance=RequestContext(request))
        out['ticket_forms'].append(form_response)

    if out['success']:
        # get all the models ready
        sale = Sale()
        sale.event = CURRENT_EVENT
        sale.amount = 0
        sale.save()

        tickets = []
        for ticket_form in valid_tickets:
            ticket = ticket_form.save(commit=False)
            ticket.event = CURRENT_EVENT
            ticket.sale = sale
            ticket.save()

            tickets.append(ticket)

        # tickets are good, so confirm price
        price = get_price_data(tickets=types, coupon=data.get('coupon', None))

        if price['price'] > 0:
            # there should have been a payment form
            form_response = {'success': True}
            payment_form = PaymentForm(data.get('payment_form', {}))
            if payment_form.is_valid():
                # do the prices match?
                if price['price'] != float(data['expected_price']):
                    # something is wrong
                    payment_form._errors["__all__"] = ErrorList([u"There was a problem calculating your payment due. Please contact us for further assistance."])
                    form_response['success'] = False
                else:
                    # moving onward -- update all the sale forms with the relevant fields
                    sale.first_name = payment_form['first_name'].value()
                    sale.last_name = payment_form['last_name'].value()
                    sale.email = payment_form['email'].value()

                    sale.address1 = payment_form['address1'].value()
                    sale.address2 = payment_form['address2'].value()
                    sale.city = payment_form['city'].value()
                    sale.state = payment_form['state'].value()
                    sale.zip = payment_form['zip'].value()

                    sale.amount = price['price']
                    if 'coupon' in price:
                        coupon_code = CouponCode.objects.get(event=CURRENT_EVENT, code=price['coupon'])

                    sale.save()

                    # now pay for things
                    result = braintree.Transaction.sale({
                        "amount": "%.2f" % price['price'],
                        "credit_card": {
                            "number": payment_form["number"].value(),
                            "cvv": payment_form["cvv"].value(),
                            "expiration_month": payment_form["exp_month"].value(),
                            "expiration_year": payment_form["exp_year"].value()
                        },
                        "options": {
                            "submit_for_settlement": True
                        }
                    })

                    if result.is_success:
                        # everything is wonderful!
                        # save everything
                        sale.success = True
                        sale.transaction_id = result.transaction.id
                        sale.save()

                        for ticket in tickets:
                            ticket.success = True
                            ticket.save()
                    else:
                        if hasattr(result.transaction, "id"):
                            sale.transaction_id = result.transaction.id
                            sale.save()
                        payment_form._errors["__all__"] = ErrorList([u"There was a problem processing your payment: %s" % result.message])
                        form_response['success'] = False
            else:
                form_response['success'] = False
                
            if not form_response['success']:
                out['success'] = False
                form_response['text'] = render_to_string('reg/partials/payment_form.html', {'payment_form': payment_form}, context_instance=RequestContext(request))
            out['payment_form'] = form_response
        else:
            # we can blindly accept all the tickets, since no payment is necessary
            sale.success = True
            sale.save()

            for ticket in tickets:
                ticket.success = True
                ticket.save()

    return HttpResponse(json.dumps(out), content_type="application/json")


@csrf_exempt
def price_check(request):
    if request.method != "POST":
        raise Http404
    try:
        data = json.loads(request.raw_post_data)
    except:
        raise Http404

    return HttpResponse(json.dumps(get_price_data(tickets=data['tickets'], coupon=data.get('coupon', None))), content_type="application/json")

# utilities
def get_price_data(tickets={}, coupon=None):
    out = {}
    total = 0
    for tk, qty in tickets.items():
        ticket = TicketType.objects.get(id=tk)
        total += float(ticket.price) * qty

    if coupon:
        try:
            cp = CouponCode.objects.get(event=CURRENT_EVENT, code=coupon)
            out['coupon'] = coupon
            total -= (cp.discount / 100.0) * total
        except:
            out['coupon_error'] = "Coupon code not found."
    out['price'] = total

    return out

# payment
braintree.Configuration.configure(
    braintree.Environment.Production if getattr(settings, "BRAINTREE_ENVIRONMENT", "sandbox") == "production" else braintree.Environment.Sandbox,
    merchant_id=getattr(settings, "BRAINTREE_MERCHANT_ID", ""),
    public_key=getattr(settings, "BRAINTREE_PUBLIC_KEY", ""),
    private_key=getattr(settings, "BRAINTREE_PRIVATE_KEY", "")
)