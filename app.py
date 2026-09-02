from flask import Flask
app = Flask(__name__)

@app.route('/')
def hello_world():
    return """
<!DOCTYPE html>
<html lang="en">

<body>
    <div class="container" style="bg-dark text-red text-center py-3 mt-5">
        <a href="https://github.com/nikhilsainiop" class="card">
            <p>
	    <center>
	        <br
              /><br
              />▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄<br 
	      />██░▄▄▄░█░▄▄▀█▄░▄██░▀██░█▄░▄██<br 
              />██▄▄▄▀▀█░▀▀░██░███░█░█░██░███<br 
	      />██░▀▀▀░█░██░█▀░▀██░██▄░█▀░▀██<br 
              />▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀<br 
	      /><br
              /><br>
                <b>Powered By SAINI BOTS</b>
		</center>
            </p>
        </a>
    </div>
	<br></br>
        <center>
	<footer class="bg-dark text-white text-center py-3 mt-5">
		<div class="footer__copyright">
            <p class="footer__copyright-info">
                © 2025 Video Downloader. All rights reserved.
            </p>
        </div>
    </footer>
    </center>
</body>

</html>
"""


if __name__ == "__main__":
    app.run()
