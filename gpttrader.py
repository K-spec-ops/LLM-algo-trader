# LLM enabled options trading bot
# Don't put syncronous event driven calls into IBKR callbacks!! (like accountSummary).
# Always do these event calls in your own functions

# Two threads:
   # app.run() handles IBKR callbacks
   # app.starter() handles trading logic (i.e. everything besides callbacks)

from importscript import * 
from launcher import * 

def args():
    desc= """In order to use this script, you must set the OPEN_AI_KEY environment variable
to your OpenAI API key. You can do this by running the following commands in your terminal:
            
    1. echo \"export OPEN_AI_KEY='<your_api_key_here>'\" >> ~/.bashrc
    2. source ~/.bashrc 

You can check that the environment variable is set correctly using: echo $OPEN_AI_KEY."""
    parser= argparse.ArgumentParser(prog= os.path.basename(__file__), description= "A simple script to trade options contracts recommended by GPT using the IBKR TWS API.",
                                    epilog= desc, formatter_class= argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-p",  "--port", type= int, default= 7496, help= "Port that API listens on; default 7496 (live trading)")
    parser.add_argument("-sl",  "--stop-loss", type= float, default= 0.3, help= "Decimal percentage below fill price to place stop loss order; default 0.3")
    parser.add_argument("-tp",  "--take-profit", type= float, default= 0.5, help= "Decimal percentage above fill price to place take profit order; default 0.5")
    parser.add_argument("-n", "--num", type= int, default= 1, help= "Number of trading iterations for the script to run; default 1")
    parser.add_argument("-t", "--time", type= float, default= 48.0, help= "If '-n' is larger than 1, this is the time in hours the script waits between iterations; default 48")
    parser.add_argument("-m", "--min", type= float, default= 200.0, help= "The account balance below which the script will exit prematurely; default 200")

    return parser, parser.parse_args()

class MyFormatter(logging.Formatter):
    
    init(autoreset= True)
    error_fmt= f"{Back.RED}%(levelname)s - %(message)s{Style.RESET_ALL}"
    warn_fmt= f"{Back.YELLOW}%(levelname)s - %(message)s{Style.RESET_ALL}"
    
    def __init__(self, fmt= "%(levelname)s - %(message)s"):
        super().__init__(fmt)

    def format(self, record):
        orig_fmt= self._style._fmt # regular logging for the other levels

        if record.levelno== logging.CRITICAL or record.levelno== logging.ERROR:
            self._style._fmt= MyFormatter.error_fmt
        if record.levelno== logging.WARNING:
            self._style._fmt= MyFormatter.warn_fmt
    
        final= super().format(record)
        self._style._fmt= orig_fmt

        return final

date= datetime.now().strftime("%Y %m %d %I%M").split(" ")

logger= logging.getLogger(__name__)
logger.setLevel(logging.INFO)
streamhandler= logging.StreamHandler(sys.stdout)
filehandler= logging.FileHandler(filename= f"GPTtrader_{date[0]}_{date[1]}_{date[2]}.log")
streamhandler.setFormatter(MyFormatter())
filehandler.setFormatter(MyFormatter())
logger.addHandler(streamhandler)
logger.addHandler(filehandler)

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
            logger.error(f"The call to {func.__name__} did not complete in time. Moving on to the next order...")
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
        if self.buyingpower< money:
            logger.info(f"Your account balance is too low to continue trading: {self.buyingpower}")
            self.shutdown.set()
        return
    
    def accountSummaryEnd(self, reqId):
        self.event.set()
        return

    def order(self):
        response= self.LLMoptpicker(self.buyingpower)
        oj= json.loads(response.output_text)
        logger.info(f"This is the GPT result: {oj}")
        for i, entry in enumerate(oj, 1):
            self.i= i
            logger.info(f"Order {i} started: {entry['ticker']}")

            contract= self.buildcontract(entry)
            if not contract:
                logger.warning(f"Order {i} contract couldn't be built. This order will be skipped.")
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
                logger.warning(f"Order {i} couldn't fill in {self.fill_timeout/60:.2f} minutes." \
                                   "This order will be skipped.")
                continue
                
            stop_loss= Order()
            stop_loss.orderId= self.stoplossorderid
            stop_loss.action= "SELL" if parent.action== "BUY" else "BUY"
            stop_loss.orderType= "STP LMT"
            if parent.action== "BUY":
                stop_loss.auxPrice= round(self.avgfillprice*(1-sl), 2)
                stop_loss.lmtPrice= round(self.avgfillprice*((1-sl)-0.05), 2)
            else:
                stop_loss.auxPrice= round(self.avgfillprice*(1+sl), 2)
                stop_loss.lmtPrice= round(self.avgfillprice*((1+sl)+0.05), 2) # the lowest it will go is 5% lower than the stop loss price
            stop_loss.totalQuantity= totalquant
            stop_loss.tif= "GTC"
            stop_loss.ocaGroup= f"ocasltp_{i}"
            stop_loss.ocaType= 1

            profit_taker= Order()
            profit_taker.orderId= self.profittakerorderid
            profit_taker.action= "SELL" if parent.action== "BUY" else "BUY"
            profit_taker.orderType= "LMT"
            if parent.action== "BUY":
                profit_taker.lmtPrice= round(self.avgfillprice*(1+tp), 2)
            else:
                profit_taker.lmtPrice= round(self.avgfillprice*(1-tp), 2) 
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

        if iter> 1:
            while time()< wait*3600:
                sleep(0.1)
            logger.info(f"I've finished waiting {wait} hours. Moving on to the next iteration...")
        
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
                
                logger.info(f"GPT strike and expiration: {strike}, {date}\n" \
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
                    logger.warning(f"The 'legs' parameter of the {ent['ticker']} order is an empty list." /
                                  f"This order will be skipped.")
                    return
        else:
            logger.warning(f"Invalid format for the 'legs' parameter of the {ent['ticker']} order." /
                          f"This was given: {ent['legs']}. This order will be skipped.")
            return
        
        mycontract.comboLegs= []
        mycontract.comboLegs.extend(list(self.legs.values())) if self.legs else None
        
        logger.info("Executing legs: "+ message)
    
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
            logger.info(f"Order {self.i} stop loss has been submitted!")
            self.event.set()
        if orderId== self.profittakerorderid and status== "Submitted":
            logger.info(f"Order {self.i} profit taker has been submitted!")
            self.event.set()
        if orderId== self.stoplossorderid and status== "Filled":
            logger.info(f"Order {self.i} stop loss has been filled! Returned {((avgFillPrice/self.avgfillprice)-1)*100:.2f}")
        if orderId== self.profittakerorderid and status== "Filled":
            logger.info(f"Order {self.i} profit taker has been filled! Returned {((avgFillPrice/self.avgfillprice)-1)*100:.2f}")

        return

    def LLMoptpicker(self, balance):
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
        logger.info("Running GPT query, this may take a few minutes...")
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

        logger.info(f"The openAI api call took {end:.2f} seconds.")

        return response
    
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="", errorDetails="", *args):
        if reqId== -1:
            return
        if errorCode not in (2104, 2106, 2158): 
            logger.error(f"Request {reqId} failed with error code {errorCode}. {errorString}.")
            logger.error(f"JSON: {advancedOrderRejectJson}")
            logger.error(f"Details: {errorDetails}")
            self.shutdown.set()
            self.disconnect()

parse, arg= args()

try:
    api= os.environ["OPEN_AI_KEY"]
except KeyError:
    logger.error("You must set the OPEN_AI_KEY environment variable to your OpenAI API key.")
    sys.exit(1)

port, sl, tp, iter, wait, money= arg.port, arg.stop_loss, arg.take_profit, arg.num, arg.time, arg.min

app= TestApp()
tries= 5 # internal param

logger.info("Welcome to the IBKR GPT Trading Bot!")

for att in range(1, tries+1):
    logger.info(f"Attempting to connect to TWS, Try {att}")
    app.connect("127.0.0.1", port, 1)
    if app.isConnected():
        logger.info("API connection to TWS has been established!")
        break
    sleep(1)
else:
    logger.error("The API could not connect to TWS.")
    sys.exit(1)

threading.Thread(target= app.run, daemon= True).start()
if not app.setup.wait(timeout= 10):
    logger.error("Did not receive nextValidId from TWS.")
    app.disconnect()
    sys.exit(1)

def counter(lim):
    i= 1
    while i<= lim:
        yield i 
        i+= 1

def main():
    logger.info("The API is ready!")

    for att in counter(iter):
        if app.shutdown.is_set():
            break

        logger.info(f"Starting on iteration {att}...")
        app.starter()
    
    app.disconnect()
    return

if __name__=="__main__":
    main()
    logger.info("I've finished trading! Until next time...")

