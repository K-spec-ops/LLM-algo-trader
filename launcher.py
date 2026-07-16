# Launch the GPT trader script and deal with EC2 provisioning

from importscript import *

def args():
    desc= """In order to use this script, you must set the OPEN_AI_KEY environment variable
to your OpenAI API key. You can do this by running the following commands in your terminal:
            
    1. echo \"export OPEN_AI_KEY='<your_api_key_here>'\" >> ~/.bashrc
    2. source ~/.bashrc 

You can check that the environment variable is set correctly using: echo $OPEN_AI_KEY."""
    parser= argparse.ArgumentParser(prog= os.path.basename(__file__), description= "A simple script to trade options contracts recommended by GPT using the IBKR TWS API.",
                                    epilog= desc, formatter_class= argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-p",  "--port", type= int, default= 7496, help= "Port that API listens on; default 7496 (live trading)")
    parser.add_argument("-sl",  "--loss", type= float, default= 0.3, help= "Decimal percentage below fill price to place stop loss order; default 0.3")
    parser.add_argument("-tp",  "--profit", type= float, default= 0.5, help= "Decimal percentage above fill price to place take profit order; default 0.5")
    parser.add_argument("-n", "--num", type= int, default= 1, help= "Number of trading iterations for the script to run; default 1")
    parser.add_argument("-t", "--itime", type= float, default= 48.0, help= "If '-n' is larger than 1, this is the time in hours the script waits between iterations; default 48")
    parser.add_argument("-m", "--minm", type= float, default= 200.0, help= "The account balance below which the script will exit prematurely; default 200")
    parser.add_argument("-c", "--cont", action= "store_true", default= None, help= "Run iterations on an EC2 instance; default False, autoselects True for '-n'>= 10")

    return parser.parse_args()

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

def make_logger(name, level, filename= None):
    logger= logging.getLogger(name)
    logger.setLevel(level)
    if filename:
        filehandler= logging.FileHandler(filename= filename)
        filehandler.setFormatter(MyFormatter())
        logger.addHandler(filehandler)
    streamhandler= logging.StreamHandler(sys.stdout)
    streamhandler.setFormatter(MyFormatter())
    logger.addHandler(streamhandler)

    return logger

date= datetime.now().strftime("%Y %m %d %I%M").split(" ")
fn= f"GPTtrader_{date[0]}_{date[1]}_{date[2]}.log"

arg= args()
globals().update(vars(arg)) # dynamically assign args to variables
parlog= make_logger(__name__, logging.INFO, fn if cont else None) # type: ignore
parlog.info(f"Your log file is: {os.path.abspath(fn+ '.log') if fn else 'None Specified'}")

def run_wrapper(command, tout):
    p= subprocess.Popen(command)
    end= time()+ tout
    while time()< end:
        code= p.poll()
        parlog.debug(f"sub-process has a return code of {code}")
        if code== 0:
            break
        sleep(0.5)
    else:
        parlog.debug(f"sub-process didn't complete in {tout} seconds. Exiting...")
        p.terminate()
        try:
            p.wait(timeout= 10)
        except subprocess.TimeoutExpired:
            p.kill()
    
    return code

try:
    api= os.environ["OPEN_AI_KEY"]
except KeyError:
    logger.error("You must set the OPEN_AI_KEY environment variable to your OpenAI API key.")
    sys.exit(1)

if num> 10: # type: ignore
    parlog.info("EC2 will be used since the number of iterations is larger than 10.")
    cont= True
if cont:
    _= make_logger("boto3", logging.INFO, fn)
    _= make_logger("botocore", logging.INFO, fn)
    try:
        boto3.client("sts").get_caller_identity()
    except (ClientError, NoCredentialsError, CredentialRetrievalError):
        parlog.error("Your AWS credentials are invalid, expired, or missing!")
        sys.exit(1)
    ec2, mgh= boto3.client("ec2"), boto3.client("mgh")
    ami= "ami-01edba92f9036f76e" # Amazon Linux 2023 kernel-6.18 AMI, until June 2029
    info= ec2.run_instances(ImageId= ami, 
                         InstanceType= "t2.small",
                         MaxCount= 1,
                         MinCount= 1,
                         Placement= {"AvailabilityZone": "us-east-1a"},
                         InstanceInitiatedShutdownBehavior= "terminate",
                         UserData= '''#!/bin/bash 
                            sudo dnf install git -y 
                            git clone https://github.com/K-spec-ops/LLM-algo-trader.git
                            cd LLM-algo-trader
                            pip install -r requirements.txt
                            python gpttrader.py
                            shutdown now''')["Instances"][0]
    
    instanceid, arch, state, typ, avail= info["InstanceId"], info["Architecture"], info["State"]["Name"], info["InstanceType"], info["Placement"]["AvailabilityZone"]
    platform= ec2.describe_images(ImageIds=[ami])["Images"][0]["PlatformDetails"]
    ec2.get_waiter("instance_exists").wait(InstanceIds= [instanceid])
    
    parlog.info(f'''Instance {instanceid} has launched! DETAILS: 
                                                  Architechure: {arch}
                                                  State: {state}
                                                  Platform: {platform}
                                                  Instance type: {typ}
                                                  A-Zone: {avail}''')
    parlog.info("Shutting down local script. Continuing on EC2...")
    sys.exit(1)

else:
    code_2= run_wrapper(["python", "gpttrader.py"], 300)
    if not code_2== 0:
        parlog.error(f"Couldn't launch the trader script! Subprocess error code: {code_2}")
        sys.exit(1)

