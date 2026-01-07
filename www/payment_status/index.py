import frappe
import json
import ast
from frappe.utils import fmt_money

def get_context(context):
    # Ambil query param
    client_ref = frappe.form_dict.paymentRequestId

    #jika tidak ditemukan return error
    if not client_ref:
        context.error = "paymentRequestId tidak ditemukan"
        return

    try:
        # Ambil data dari doctype
        doc = frappe.get_doc("Pivot Payment Request", client_ref)
        campaign_id = doc.campaign_id
        
        # Ambil data dari doctype 
        campaign = frappe.get_doc("Fundraising Campaign", campaign_id)
        doc.campaign_id = campaign.campaign_name

        context.doc = doc

    except Exception as e:
        context.error = str(e)
