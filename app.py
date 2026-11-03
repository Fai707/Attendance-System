from flask import Flask, render_template, request

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/attend")
def attend():
    student_id = request.args.get("student_id")
    otp = request.args.get("otp")
    print(student_id, otp)
    return render_template("index.html", student_id=student_id, otp=otp)

app.run(port=5002)