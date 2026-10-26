import datetime as dt
import io
import csv

import pandas as pd
import streamlit as st

import ui
from core import aba, db, ledger, security
from core.money import fmt, to_cents

ui.guard(admin=True)
ui.mode_banner()
st.title("Vault E-Terminal")
st.caption(f"Reference on every manual transfer: **{ledger.REFERENCE}**")
me = ui.username()
s = ledger.summary()

c = st.columns(3) + st.columns(2)
c[0].metric("Reserve ceiling", fmt(s["ceiling_cents"]))
c[1].metric("Vault balance", fmt(s["balance_cents"]))
c[2].metric("Available to release", fmt(s["available_cents"]))
c[3].metric("Unverified deposits", fmt(s["unverified_cents"]))
c[4].metric("Room under ceiling", fmt(s["headroom_cents"]))

t_rel, t_list, t_dep, t_exp, t_ceil = st.tabs(["Manual release", "Releases", "Deposits", "Export direct credit", "Reserve ceiling"])

# ---------------- manual release ----------------
with t_rel:
    st.subheader("Create a Vault Payment Plt release")
    st.caption("Pay by BSB and account number with the account name, or by PayID. A draft does not move money. You approve it in the Releases tab.")
    saved = db.q("SELECT * FROM payees ORDER BY name")
    labels = {f"{p['name']} · {p['bsb']} {p['account']}": p for p in saved}

    def _fill():
        p = labels.get(st.session_state.get("saved_payee"))
        st.session_state["rel_name"] = p["name"] if p else ""
        st.session_state["rel_bsb"] = p["bsb"] if p else ""
        st.session_state["rel_acct"] = p["account"] if p else ""
        st.session_state["rel_payid"] = (p["payid"] or "") if p else ""

    st.selectbox("Saved payee (optional)", ["New payee"] + list(labels), key="saved_payee", on_change=_fill,
                 help="People you have paid before. Choosing one fills the details below.")
    with st.form("release"):
        name = st.text_input("Payee full account name", key="rel_name")
        a, b = st.columns(2)
        bsb = a.text_input("BSB (e.g. 062-000)", key="rel_bsb")
        acct = b.text_input("Account number", key="rel_acct")
        payid = st.text_input("or PayID (email, mobile or ABN)", key="rel_payid")
        amt = st.text_input("Amount (AUD)", placeholder="0.00")
        memo = st.text_input("Internal note (optional)")
        confirm = st.checkbox("I have checked the payee details and any warnings shown")
        go = st.form_submit_button("Create draft")
    if go:
        try:
            cents = to_cents(amt)
            rid = ledger.create_release(name, bsb, acct, payid, cents, memo, me, confirmed_warnings=confirm)
            st.success(f"Draft #{rid} created. Go to the Releases tab to approve it.")
        except ValueError as e:
            msg = str(e)
            (st.warning if msg.startswith("CONFIRM") else st.error)(msg + (" Tick the confirmation box and submit again." if msg.startswith("CONFIRM") else ""))

# ---------------- releases ----------------
with t_list:
    rows = ledger.releases()
    if not rows:
        st.info("No releases yet.")
    for r in rows:
        head = f"#{r['id']} · {r['status']} · {fmt(r['amount_cents'])} · {r['payee_name']} · {r['reference']}"
        with st.expander(head, expanded=(r["status"] == "DRAFT")):
            idx = ledger.step_index(r["status"])
            if idx >= 0:
                st.markdown("  →  ".join(("✅ " if i <= idx else "⬜ ") + s_ for i, s_ in enumerate(ledger.STEPS)))
            else:
                st.markdown("🚫 Cancelled")
            st.write(f"BSB/Account: **{r['bsb'] or '-'} / {r['account'] or '-'}**  ·  PayID: **{r['payid'] or '-'}**")
            st.write(f"Created {ui.ts(r['ts'])} by {r['created_by']}. {('Approved by ' + r['approved_by'] + ' ' + ui.ts(r['approved_ts'])) if r['approved_by'] else ''}")
            if r["memo"]:
                st.caption(r["memo"])
            if r["status"] == "DRAFT":
                if ui.password_ok(f"pw{r['id']}", "Re-enter your password to approve"):
                    if st.button("Approve and commit funds", key=f"ap{r['id']}", type="primary"):
                        try:
                            ledger.approve_release(r["id"], me)
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                if st.button("Cancel draft", key=f"cd{r['id']}"):
                    ledger.cancel_release(r["id"], me); st.rerun()
            elif r["status"] in ("APPROVED", "EXPORTED"):
                st.info("Funds are committed in the ledger. Send the payment from your bank or provider (Export tab), then mark it paid once the bank confirms.")
                x1, x2 = st.columns(2)
                if x1.button("Mark paid (bank confirmed)", key=f"sp{r['id']}"):
                    ledger.mark_settled(r["id"], me); st.rerun()
                if x2.button("Cancel and return funds", key=f"cx{r['id']}"):
                    ledger.cancel_release(r["id"], me); st.rerun()

# ---------------- deposits ----------------
with t_dep:
    st.subheader("Record money that has arrived in the reserve account")
    st.caption("Only record real deposits. Enter the bank or provider reference so it can be matched to your statement. The ceiling is a cap, it never adds money.")
    with st.form("dep"):
        amt = st.text_input("Amount received (AUD)")
        ref = st.text_input("Bank or provider reference")
        memo = st.text_input("Note")
        if st.form_submit_button("Record deposit"):
            try:
                ledger.record_deposit(to_cents(amt), ref, memo, me)
                st.success("Deposit recorded." + ("" if ledger.mode() == "TEST" else " It becomes available after you verify it below."))
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    deps = [r for r in ledger.ledger_rows() if r["kind"] == "DEPOSIT"]
    for d in deps:
        c1, c2, c3 = st.columns([3, 2, 2])
        c1.write(f"{ui.ts(d['ts'])} · **{fmt(d['amount_cents'])}** · ref {d['ref']}")
        c2.write("✅ verified" if d["verified"] else "⏳ unverified")
        if not d["verified"] and c3.button("Mark verified against bank statement", key=f"vf{d['id']}"):
            ledger.verify_deposit(d["id"], me); st.rerun()

# ---------------- export ----------------
with t_exp:
    st.subheader("Send approved releases as a bank direct-credit file (ABA)")
    st.caption("Upload the file in your bank's business internet banking (bulk payments / direct entry). Your bank moves the money. PayID payments cannot be in an ABA file, so they are listed for you to send from your bank app.")
    with st.expander("Your bank file details", expanded=not db.get_setting("aba_own_bsb")):
        with st.form("abacfg"):
            g = lambda k, d="": db.get_setting(k, d)
            bank = st.text_input("Bank abbreviation (3 letters, e.g. CBA, WBC, NAB, ANZ)", g("aba_bank", "CBA"))
            uname = st.text_input("Account name (user name on the file, max 26 chars)", g("aba_user"))
            apca = st.text_input("APCA user ID (6 digits, from your bank)", g("aba_apca"))
            desc = st.text_input("File description (max 12 chars)", g("aba_desc", "VAULTPAY"))
            obsb = st.text_input("Your account BSB", g("aba_own_bsb"))
            oacc = st.text_input("Your account number", g("aba_own_acc"))
            rem = st.text_input("Remitter name (max 16 chars)", g("aba_rem"))
            if st.form_submit_button("Save"):
                try:
                    security.norm_bsb(obsb); security.norm_account(oacc)
                    if not apca.isdigit() or len(apca) > 6:
                        raise ValueError("APCA user ID must be up to 6 digits.")
                    for k, v in dict(aba_bank=bank, aba_user=uname, aba_apca=apca, aba_desc=desc, aba_own_bsb=obsb, aba_own_acc=oacc, aba_rem=rem).items():
                        db.set_setting(k, v)
                    security.audit(me, "ABA_SETTINGS_SAVED", "")
                    st.success("Saved.")
                except ValueError as e:
                    st.error(str(e))
    ready = ledger.releases("APPROVED")
    bank_ready = [r for r in ready if r["bsb"] and r["account"]]
    payid_ready = [r for r in ready if r["payid"] and not (r["bsb"] and r["account"])]
    if bank_ready:
        st.write(f"**{len(bank_ready)}** approved bank-account release(s), total **{fmt(sum(r['amount_cents'] for r in bank_ready))}**")
        if st.button("Build ABA file"):
            try:
                text = aba.build_aba(bank_ready, bank_abbrev=db.get_setting("aba_bank", "CBA"), user_name=db.get_setting("aba_user", ""),
                                     apca_id=db.get_setting("aba_apca", "0"), description=db.get_setting("aba_desc", "VAULTPAY"),
                                     own_bsb=db.get_setting("aba_own_bsb", ""), own_account=db.get_setting("aba_own_acc", ""),
                                     remitter=db.get_setting("aba_rem", ""))
                batch = dt.datetime.now().strftime("%Y%m%d%H%M%S")
                ledger.mark_exported([r["id"] for r in bank_ready], "ABA", batch, me)
                st.session_state["aba_file"] = (f"vault_payment_plt_{batch}.aba", text)
            except ValueError as e:
                st.error(str(e))
        if "aba_file" in st.session_state:
            fn, text = st.session_state["aba_file"]
            st.download_button("Download " + fn, text, file_name=fn, mime="text/plain")
    else:
        st.info("No approved bank-account releases waiting.")
    if payid_ready:
        st.write("**PayID releases to send from your bank app or provider** (reference: Vault Payment Plt):")
        ui.show_df(pd.DataFrame([{"Release": r["id"], "Payee": r["payee_name"], "PayID": r["payid"], "Amount": fmt(r["amount_cents"]), "Reference": r["reference"]} for r in payid_ready]), hide_index=True)
        if st.button("Mark PayID releases as sent"):
            ledger.mark_exported([r["id"] for r in payid_ready], "PAYID", dt.datetime.now().strftime("%Y%m%d%H%M%S"), me); st.rerun()

# ---------------- ceiling ----------------
with t_ceil:
    st.subheader("Reserve ceiling")
    st.write(f"Current ceiling: **{fmt(s['ceiling_cents'])}**. It caps how much the vault may hold. It does not create money: the balance only ever comes from deposits you record.")
    new = st.text_input("New ceiling (AUD)", value=f"{s['ceiling_cents'] // 100}")
    if ui.password_ok("pwceil"):
        if st.button("Change ceiling"):
            try:
                c2 = to_cents(new)
                if c2 < s["balance_cents"]:
                    raise ValueError("Ceiling cannot be lower than the current balance.")
                ledger.set_ceiling(c2, me); st.success("Ceiling updated."); st.rerun()
            except ValueError as e:
                st.error(str(e))
