# LLM enabled options trading bot
# Don't put syncronous event driven calls into IBKR callbacks!! (like accountSummary).
# Always do these event calls in your own functions

# Two threads:
   # app.run() handles IBKR callbacks
   # app.starter() handles trading logic (i.e. everything besides callbacks)
 
from launcher import * 

class TestApp(EClient, EWrapper):
    def __init__(self):
        EClient.__init__(self, self)
        self.timeout= 600
        self.shutdown= threading.Event()
        self.setup= threading.Event()
        self.event= threading.Event()
    
    def event_wrapper(self, func, timeout= None, **params):
        self.event.clear()
        func(**params)
        waiter= self.event.wait(timeout)
        if not waiter:
            parlog.error(f"The call to {func.__name__} did not complete in time. Moving on to the next order...")
            self.disconnect()
        return
    
    def nextId(self):
        self.orderId += 1
        return self.orderId
    
    def nextValidId(self, orderId):
        self.orderId= orderId # no duplicate orders
        self.setup.set()
        return
    
    def starter(self):
        self.event_wrapper(self.reqAccountSummary, self.timeout,
                           reqId=self.nextId(),
                           groupName="All",
                           tags="BuyingPower")
        
        if self.shutdown.is_set():
            return
        
        self.order()
        return
    
    def accountSummary(self, reqId, account, tag, value, currency):
        self.buyingpower= float(value)
        if self.buyingpower< minm: # pyright: ignore[reportUndefinedVariable]
            parlog.info(f"Your account balance is too low to continue trading: {self.buyingpower}")
            self.shutdown.set()
        return
    
    def accountSummaryEnd(self, reqId):
        self.event.set()
        return

    def order(self):
        response= self.LLMoptpicker(self.buyingpower)
        oj= json.loads(response.output_text)
        parlog.info(f"This is the GPT result: {oj}")
        for i, entry in enumerate(oj, 1):
            self.i= i
            parlog.info(f"Order {i} started: {entry['ticker']}")

            contract= self.buildcontract(entry)
            if not contract:
                parlog.warning(f"Order {i} contract couldn't be built. This order will be skipped.")
                continue
                
            totalquant= entry["totalquantity"]
            self.parentorderid= self.nextId()
            self.stoplossorderid= self.nextId()
            self.profittakerorderid= self.nextId()

            parent= Order()
            parent.orderId= self.parentorderid
            parent.action= entry["overall"]
            parent.orderType= "MKT"
            parent.tif= "DAY"
            parent.totalQuantity= totalquant
            parent.smartComboRoutingParams= []
            parent.smartComboRoutingParams.append(TagValue("NonGuaranteed", "1"))

            self.placeOrder(parent.orderId, contract, parent)

            filled= self.event.wait(timeout= self.timeout)
            self.event.clear()
            if not filled:
                parlog.warning(f"Order {i} couldn't fill in {self.fill_timeout/60:.2f} minutes." \
                                   "This order will be skipped.")
                continue
                
            stop_loss= Order()
            stop_loss.orderId= self.stoplossorderid
            stop_loss.action= "SELL" if parent.action== "BUY" else "BUY"
            stop_loss.orderType= "STP LMT"
            if parent.action== "BUY":
                stop_loss.auxPrice= round(self.avgfillprice*(1-loss), 2) # pyright: ignore[reportUndefinedVariable]
                stop_loss.lmtPrice= round(self.avgfillprice*((1-loss)-0.05), 2) # pyright: ignore[reportUndefinedVariable]
            else:
                stop_loss.auxPrice= round(self.avgfillprice*(1+loss), 2) # type: ignore
                stop_loss.lmtPrice= round(self.avgfillprice*((1+loss)+0.05), 2) # type: ignore # the lowest it will go is 5% lower than the stop loss price
            stop_loss.totalQuantity= totalquant
            stop_loss.tif= "GTC"
            stop_loss.ocaGroup= f"ocasltp_{i}"
            stop_loss.ocaType= 1

            profit_taker= Order()
            profit_taker.orderId= self.profittakerorderid
            profit_taker.action= "SELL" if parent.action== "BUY" else "BUY"
            profit_taker.orderType= "LMT"
            if parent.action== "BUY":
                profit_taker.lmtPrice= round(self.avgfillprice*(1+profit), 2) # type: ignore
            else:
                profit_taker.lmtPrice= round(self.avgfillprice*(1-profit), 2)  # type: ignore
            profit_taker.totalQuantity= totalquant
            profit_taker.tif= "GTC"
            profit_taker.ocaGroup= f"ocasltp_{i}"
            profit_taker.ocaType= 1

            self.placeOrder(stop_loss.orderId, contract, stop_loss)
            self.event.wait()
            self.event.clear()

            self.placeOrder(profit_taker.orderId, contract, profit_taker)
            self.event.wait()
            self.event.clear()

        if num> 1: # type: ignore
            while time()< iwait*3600: # type: ignore
                sleep(0.1)
            parlog.info(f"I've finished waiting {iwait} hours. Moving on to the next iteration...") # type: ignore
        
        return

    def buildcontract(self, ent):
        message= ""
        length= len(ent["legs"])
        self.legs= {}

        mycontract= Contract()
        mycontract.symbol= ent["ticker"]
        mycontract.secType= "OPT" 
        mycontract.exchange= "SMART"
        mycontract.currency= "USD"

        self.event_wrapper(self.reqContractDetails, self.timeout, reqId= self.nextId(), contract= mycontract)
        self.event_wrapper(self.reqSecDefOptParams, self.timeout, reqId= self.nextId(),
                                underlyingSymbol= mycontract.symbol,
                                futFopExchange= "",
                                underlyingSecType= "STK",
                                underlyingConId= self.conid)

        if isinstance(ent["legs"], list):
            for i, leg in enumerate(ent["legs"]):

                date= leg["date"]
                strike= int(leg["strike"])
                action= leg["action"]
                direction= leg["direction"]
                quantity= int(leg["quantity"])

                con_exp= date if date in self.expirations else min(self.expirations, 
                                                                   key= lambda x: abs(int(x)-int(date))) # find closest exp
                con_strike= strike if strike in self.strikes else min([(s, abs(s-strike)) 
                                                                       for s in self.strikes], key=lambda x:x[1])[0] # find closest date
                
                parlog.info(f"GPT strike and expiration: {strike}, {date}\n" \
                      f"Closest available strike and expiration: {con_strike}, {con_exp}")
                
                message+= f"{quantity} {mycontract.symbol} {con_strike} {con_exp} {direction}\n"
                
                mycontract.lastTradeDateOrContractMonth= con_exp
                mycontract.strike= con_strike
                mycontract.right= direction
                mycontract.multiplier= self.multiplier

                if length> 1: # multi-leg order
                    mycontract.secType= "BAG" # redundant but it looks better
                    
                    self.event_wrapper(self.reqContractDetails, self.timeout, reqId= self.nextId(), contract= mycontract)                    

                    cleg= ComboLeg()
                    cleg.conId= self.conid
                    cleg.ratio= quantity
                    cleg.action= action
                    cleg.exchange= "SMART"
                    self.legs[i]= cleg
                elif length!= 1:  
                    parlog.warning(f"The 'legs' parameter of the {ent['ticker']} order is an empty list." /
                                  f"This order will be skipped.")
                    return
        else:
            parlog.warning(f"Invalid format for the 'legs' parameter of the {ent['ticker']} order." /
                          f"This was given: {ent['legs']}. This order will be skipped.")
            return
        
        mycontract.comboLegs= []
        mycontract.comboLegs.extend(list(self.legs.values())) if self.legs else None
        
        parlog.info("Executing legs: "+ message)
    
        return mycontract
    
    def contractDetails(self, reqId, contractDetails):
        self.conid= contractDetails.contract.conId
        return
    
    def contractDetailsEnd(self, reqId):
        self.event.set()
        return
    
    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId, tradingClass, multiplier, expirations, strikes):
        self.expirations= expirations
        self.multiplier= multiplier
        self.strikes= strikes
        return

    def securityDefinitionOptionParameterEnd(self, reqId):
        self.event.set()
        return

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        # if orderId== self.parentorderid and status== "Filled":
        #     self.avgfillprice= avgFillPrice
        #     self.event.set()
        if orderId== self.parentorderid and status== "Submitted":
            self.avgfillprice= mktCapPrice
            self.event.set()
        if orderId== self.stoplossorderid and status== "Submitted":
            parlog.info(f"Order {self.i} stop loss has been submitted!")
            self.event.set()
        if orderId== self.profittakerorderid and status== "Submitted":
            parlog.info(f"Order {self.i} profit taker has been submitted!")
            self.event.set()
        if orderId== self.stoplossorderid and status== "Filled":
            parlog.info(f"Order {self.i} stop loss has been filled! Returned {((avgFillPrice/self.avgfillprice)-1)*100:.2f}")
        if orderId== self.profittakerorderid and status== "Filled":
            parlog.info(f"Order {self.i} profit taker has been filled! Returned {((avgFillPrice/self.avgfillprice)-1)*100:.2f}")

        return

    def LLMoptpicker(self, balance): # update prompt
        client= OpenAI(api_key= api)
        prompt= f'''
        
        You are an AI-powered stock options picker. You task is to analyze recent news, earnings reports, market trends, option chain data and public/institutional sentiment to identify
        a profitable options contract(s). Do NOT consider technical analysis in your reasoning. There are three rules you must follow:

        1. The account that will be placing the trade has a balance of ${balance}. Your recommendation should not cost more than 50% of this amount UNLESS the balance is less
           than $1,000.  
        2. You are allowed to recommend single-leg and multi-leg options strategies. You are not limited to a specific quantity of contracts, so long as rule 1 is followed.
        3. Provide the option contract's ticker, strike price, the action (i.e. BUY or SELL), the strategy type, date of expiration, directionality (i.e call or put), the quantity, 
           and a brief 3-5 sentence message summarizing all points that were relevant to your conclusion. This information should be arranged into a JSON format with the following structure:

           {{{{
               {{"ticker": "SPY",
               "type": "Call",
               "overall": "BUY",
               "message": "This is a message.",
               "totalquantity": "1",
               "legs": [{{                       
                        "action": "BUY",
                        "direction": "C",
                        "strike": "690",
                        "date": "20260116",
                        "quantity": "1"}}]}},

               {{{{"ticker": "TSLA",
                "type": "Long straddle",
                "overall": "BUY",
                "message": "This is a message.",
                "totalquantity": "1",
                "legs": [{{                       
                        "action": "BUY",
                        "direction": "C",
                        "strike": "447",
                        "date": "20260116",
                        "quantity": "1"}},
                        
                        {{                       
                        "action": "BUY",
                        "direction": "P",
                        "strike": "447",
                        "date": "20260116",
                        "quantity": "2"}}]}}}}

           }}}}

           This is your only output. NO additional commentary.

        Notes:
        1. If you recommend a multi-leg strategy, there will be multiple entries in the "legs" parameter. For single-leg strategies, there will be only one. 
           The "legs" parameter should ALWAYS be a list.
        2. For single-leg strategies, the "type" parameter should always be the same as the "direction" parameter.
        3. To measure public sentiment, use web_search to parse reddit.com, x.com, and stocktwits.com. Other websites are allowed, but prioritize
           these ones. 
        4. For consistency, make sure that all JSON parameters are strings, as shown in the example above.

        '''
        parlog.info("Running GPT query, this may take a few minutes...")
        start= time()
        response= client.responses.create(
            model= "gpt-5.1",
            reasoning= {"effort": "medium"}, # change as necessary
            tools= [{"type": "web_search"}],
            tool_choice= "auto",
            include=["web_search_call.action.sources"],
            input= prompt
        )
        end= time()-start

        parlog.info(f"The openAI api call took {end:.2f} seconds.")

        return response
    
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="", errorDetails="", *args):
        if reqId== -1:
            return
        if errorCode not in (2104, 2106, 2158): 
            parlog.error(f"Request {reqId} failed with error code {errorCode}. {errorString}.")
            parlog.error(f"JSON: {advancedOrderRejectJson}")
            parlog.error(f"Details: {errorDetails}")
            self.shutdown.set()
            self.disconnect()

# port, sl, tp, iter, wait, money= arg.port, arg.stop_loss, arg.take_profit, arg.num, arg.time, arg.min

app= TestApp()
tries= 5 # internal param

parlog.info("Welcome to the IBKR GPT Trading Bot!")

for att in range(1, tries+1):
    parlog.info(f"Attempting to connect to TWS, Try {att}")
    app.connect("127.0.0.1", port, 1) # type: ignore
    if app.isConnected():
        parlog.info("API connection to TWS has been established!")
        break
    sleep(1)
else:
    parlog.error("The API could not connect to TWS.")
    sys.exit(1)

threading.Thread(target= app.run, daemon= True).start()
if not app.setup.wait(timeout= 10):
    parlog.error("Did not receive nextValidId from TWS.")
    app.disconnect()
    sys.exit(1)

def counter(lim):
    i= 1
    while i<= lim:
        yield i 
        i+= 1

def main():
    parlog.info("The API is ready!")

    for att in counter(num): # type: ignore
        if app.shutdown.is_set():
            break

        parlog.info(f"Starting on iteration {att}...")
        app.starter()
    
    app.disconnect()
    return

if __name__=="__main__":
    main()
    parlog.info("I've finished trading! Until next time...")

