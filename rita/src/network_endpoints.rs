use althea_types::{Identity, PaymentTx};
use debt_keeper::{DebtKeeper, DebtAction, DebtAdjustment};

use payment_controller::{PaymentController};
extern crate num256;
use num256::Int256;

use rouille::{Request, Response};

extern crate serde;
extern crate serde_json;

extern crate rand;

use std::sync::mpsc::Sender;
use std::sync::{Mutex, Arc};
use std::io::Read;
use std::ops::Sub;

pub fn make_payments(request: &Request,
                     m_tx: Arc<Mutex<Sender<DebtAdjustment>>>,
                     node_balance: Arc<Mutex<Int256>>,
                     pc: Arc<Mutex<PaymentController>>)
    -> Response {
    if let Some(mut data) = request.data() {
        let mut pmt_str = String::new();
        data.read_to_string(&mut pmt_str).unwrap();
        let pmt: PaymentTx = serde_json::from_str(&pmt_str).unwrap();
        trace!("Received payment, Payment: {:?}", pmt);
        m_tx.lock().unwrap().send(
            DebtAdjustment {
                ident: pmt.from,
                amount: Int256::from(pmt.amount.clone())
            }
        ).unwrap();
        let balance = (node_balance.lock().unwrap()).clone();

        pc.lock().unwrap().payment_received(pmt.clone(), balance.clone());
        *(node_balance.lock().unwrap()) = balance.clone().sub(Int256::from(pmt.amount));
        trace!("Received payment, Balance: {:?}", balance);
        Response::text("Payment Recieved")
    } else {
        Response::text("Payment Error")
    }
}