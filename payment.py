import frappe
import requests
import random
import string
import pytz
import json
from datetime import datetime, timedelta, timezone
from frappe.utils import now_datetime, get_datetime, flt, fmt_money
from erpnext_pivot.payment_gateway.payment_qr import payment_qr

@frappe.whitelist(allow_guest = True)
def create_token():
    settings = frappe.get_doc("Pivot Setting", '0de49ru9g2')

    url = 'https://api-stg.pivot-payment.com/v1/access-token'
    if (settings.nama == "Production"):
        url = 'https://api.pivot-payment.com/v1/access-token'

    payload = {
        "grantType": "client_credentials"
    }

    r = requests.post(
        url,
        json=payload,
        headers={
            "X-MERCHANT-ID": settings.merchant_id,
            "X-MERCHANT-SECRET": settings.get_password("merchant_secret")
        },
        timeout=20
    )
    # 2. Pastikan request sukses (kode status 200)
    if r.status_code != 200:
        frappe.throw(_(f"Permintaan API gagal. Status: {r.status_code}, Pesan: {r.text}"))

    try:
        response_dict = r.json() 
    except requests.exceptions.JSONDecodeError:
        frappe.throw(_("Gagal mendekode respons sebagai JSON."))

    data_payload = response_dict.get("data")
    
    if data_payload and isinstance(data_payload, dict):
        access_token = data_payload.get("accessToken")
        if access_token:
            # Simpan token baru dan waktu pembuatannya
            settings.access_token = access_token
            settings.token_generated_at = now_datetime() 
            settings.save()
            frappe.db.commit()

            return access_token
        else:
            frappe.throw(_("Key 'accessToken' tidak ditemukan dalam respons API."))
    else:
        frappe.throw(_("Key 'data' tidak ditemukan atau bukan format yang diharapkan dalam respons API."))

@frappe.whitelist(allow_guest = True)
def create_payment():

    # Only allow POST
    if frappe.request.method != 'POST':
        return {'error': 'Method not allowed'}, 405
    
    try:
        # Get data
        if frappe.request.is_json:
            data = frappe.request.get_json()
        else:
            data = frappe.form_dict
        
        # Validate required fields
        required = ['amount_value', 'name', 'email', 'campaign_id','phone_number']
        for field in required:
            if field not in data:
                frappe.local.response["http_status_code"] = 400
                return {'error': f'Missing {field}'}, 400

        reference_id = generate_reference_id()
        invoice_no = generate_invoice()
        success_url = "https://erp.siapguna.org/api/method/erpnext_pivot.payment_gateway.api.success"
        failure_url = "https://erp.siapguna.org/api/method/erpnext_pivot.payment_gateway.api.failure"
        
        doc = frappe.new_doc("Pivot Payment Request")

        doc.clientreferenceid = reference_id
        doc.invoiceno = invoice_no
        doc.customer = data['name']
        doc.phonenumber = data['phone_number']
        doc.email = data['email']
        doc.amount = flt(data['amount_value'])
        doc.paymentmethod = "QR"
        doc.successreturnurl = success_url
        doc.status = "Pending"
        doc.campaign_id = data['campaign_id']
        doc.doa = data.get('doa') or ""
        doc.dateupdate = frappe.utils.nowdate()
        doc.signature = data.get("signature") or ""

        # Simpan dokumen
        doc.insert(ignore_permissions=True)
        frappe.db.commit()

        doc_name = doc.name

        settings = frappe.get_doc("Pivot Setting", '0de49ru9g2')

        url = 'https://api-stg.pivot-payment.com/v2/payments'
        if (settings.nama == "Production"):
            url = 'https://api.pivot-payment.com/v2/payments'

        
        # Extract values
        amount = flt(data['amount_value'])
        name = data['name']
        email = data['email']
        phone = data['phone_number']
        campaign = data['campaign_id']

        payload = payment_qr(
            amount_value = amount,
            given_name = name,
            email = email,
            phone_number = phone,
            invoice_no = invoice_no,
            reference_id = reference_id,
            success_url = success_url,
            failure_url = failure_url
        )

        #print(payload);
        #return {'data': f'{settings.access_token}'}, 200

        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.access_token}",
                "X-REQUEST-ID": generate_request_id(),
                "Content-Type": "application/json",
            },
            timeout=20
        )

        r = response.json()

        if r.get("code") != "00":
            frappe.log_error(frappe.get_traceback(), 'Pivot API success, tapi chargeDetails kosong')
            return {f"Error Pivot: {r.get('message')} - {payload}"}, 500

        # Ambil qrUrl sesuai struktur respon 
        pivot_data = r.get("data") or {}
        charge_list = pivot_data.get("chargeDetails") or []
        if not charge_list:
            frappe.log_error(frappe.get_traceback(), 'Pivot API success, tapi chargeDetails kosong')
            return {'error': str(e)}, 500

        qr_info = charge_list[0].get("qr") or {}
        qr_url = qr_info.get("qrUrl")

        if not qr_url:
            frappe.log_error(frappe.get_traceback(), 'Pivot API tidak mengembalikan qrUrl')
            return {'error': str(e)}, 500

        response_json = json.dumps(r)
        #update response
        frappe.db.set_value("Pivot Payment Request", doc_name, {
            "response": response_json,
            "qr_image": qr_url
        })

        # Return agar bisa redirect payment Status
        return {
            "qr_url" : qr_url,
            "paymentStatus_url": "https://erp.siapguna.org/payment_status?paymentRequestId=" + doc_name
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Payment API Error')
        return {'error': str(e)}, 500

@frappe.whitelist(allow_guest = True)
def testing():
    data = frappe.request.get_json()
    response_json = json.dumps(data)

    try:
        event = data.get("event")
        payment = data.get("data", {})

        # field penting
        pivot_id = payment.get("id")
        client_ref = payment.get("clientReferenceId")
        amount_value = payment.get("amount", {}).get("value")
        currency = payment.get("amount", {}).get("currency")
        method_type = payment.get("paymentMethod", {}).get("type")
        status = payment.get("status")
        customer = payment.get("customer", {})
        given_name = customer.get("givenName")
        paid_at = None

        docname = frappe.db.get_value(
            "Pivot Payment Request", 
            {"clientreferenceid": client_ref}, 
            "name"
        )

        phonenumber = frappe.db.get_value(
            "Pivot Payment Request", 
            {"clientreferenceid": client_ref}, 
            "phonenumber"
        )
        
        amount = float(amount_value)

        if event == "PAYMENT.PAID" :
            if status == "PAID" :
                if amount >= 20000 and phonenumber:
                    url = "https://api.fonnte.com/send"
                    headers = {
                        "Authorization": "xxx"
                    }
                    payload = {
                        "target": phonenumber,
                        "message": f"Testing\n\nAlhamdulillah, Donasi dari {given_name}, senilai Rp. {amount_value} sudah kami terima dan tercatat.\n\nSemoga menjadi keberkahan dan jalan kebermanfaatan, serta Allah Subhanahu wa Ta'ala membalasnya dengan yang lebih baik dan berkah. Aamiin."
                    }

                    try:
                        response = requests.post(
                            url,
                            headers=headers,
                            data=payload
                        )
                        frappe.log(f"WA terkirim ke {phonenumber}: {response}")
                        return "Ok + kirim WA"
                    except Exception as e:
                        frappe.log_error(f"WA gagal terkirim ke {phonenumber}: {str(e)}")
                        return f"Error: {str(e)}"
        return "Ok"

    except Exception as e:
        frappe.log_error(f"Gagal memproses notifikasi Pivot: {e} | Data: {data}", "Pivot Payment Webhook Critical Error")
        # Berikan respons Gagal
        return f"Gagal: {e}"

@frappe.whitelist(allow_guest = True)
def callback():

    headers = frappe.request.headers

    # Cek apakah X-API-Key ada dan valid
    ValidKey = "xxx" 
    pivot_api_key = headers.get("X-API-Key")

    if pivot_api_key != ValidKey :
        frappe.local.response["http_status_code"] = 401
        return {
            "status": "error",
            "message": "Unauthorized - Invalid X-API-Key",
            "received_key": pivot_api_key
        }

    data = frappe.request.get_json()
    
    try:
        response_json = json.dumps(data)

        event = data.get("event")
        payment = data.get("data", {})

        if event == "PAYMENT.TEST" :
            return "Ok - TEST"

        # field penting
        pivot_id = payment.get("id")
        client_ref = payment.get("clientReferenceId")
        amount_value = payment.get("amount", {}).get("value")
        currency = payment.get("amount", {}).get("currency")
        method_type = payment.get("paymentMethod", {}).get("type")
        status = payment.get("status")
        customer = payment.get("customer", {})
        given_name = customer.get("givenName")
        paid_at = None

        # Ambil waktu paid dari chargeDetails jika ada
        if payment.get("chargeDetails"):
            paid_at = payment["chargeDetails"][0].get("paidAt")

        docname = frappe.db.get_value(
            "Pivot Payment Request", 
            {"clientreferenceid": client_ref}, 
            "name"
        )

        phonenumber = frappe.db.get_value(
            "Pivot Payment Request", 
            {"clientreferenceid": client_ref}, 
            "phonenumber"
        )
        
        #update status
        frappe.db.set_value("Pivot Payment Request", docname, {
            "response": response_json,
            "status": status,
            "dateupdate": now_datetime()
        })

        #send message jika status = paid & amount_value > 20.000
        min_amount_wa = 20000

        amount = float(amount_value)
        amount_label = fmt_money(amount, currency="IDR", precision=0)

        if event == "PAYMENT.PAID" :
            if status == "PAID" :
                if amount >= min_amount_wa :
                    url = "https://api.fonnte.com/send"
                    headers = {
                        "Authorization": "xxx"
                    }
                    payload = {
                        "target": phonenumber,
                        "message": f"Alhamdulillah, Donasi dari {given_name}, senilai {amount_label} sudah kami terima dan tercatat.\n\nSemoga menjadi keberkahan dan jalan kebermanfaatan, serta Allah Subhanahu wa Ta'ala membalasnya dengan yang lebih baik dan berkah. Aamiin."
                    }

                    try:
                        response = requests.post(
                            url,
                            headers=headers,
                            data=payload
                        )
                        frappe.log(f"WA terkirim ke {phonenumber}: {response}")
                        return "Ok - Send WA"
                    except Exception as e:
                        frappe.log_error(f"WA gagal terkirim ke {phonenumber}: {str(e)}")

        return "Ok"

    except Exception as e:
        frappe.log_error(f"Gagal memproses notifikasi Pivot: {e} | Data: {data}", "Pivot Payment Webhook Critical Error")
        # Berikan respons
        return f"Gagal: {e}"

def generate_reference_id(timezone_str="Asia/Jakarta"):
    # ambil timezone Asia/Jakarta (WIB)
    tz = pytz.timezone(timezone_str)
    now = datetime.now(tz)

    # format: tahun-bulan-tanggal-jam
    time_part = now.strftime("%Y%m%d%H")

    # random 4 karakter (angka + huruf besar)
    chars = string.ascii_uppercase + string.digits
    rand = ''.join(random.choices(chars, k=4))
    return f"{time_part}{rand}"

def generate_invoice():
    # random 4 karakter (angka + huruf besar)
    chars = string.ascii_uppercase + string.digits
    rand = ''.join(random.choices(chars, k=7))
    return f"INV{rand}"

def generate_request_id():
    # generate X-REQUEST-ID: 2025-12-03-12TX-F4Az
    rand = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return datetime.now().strftime("%Y%m%d%H%M-") + rand