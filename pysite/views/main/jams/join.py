import datetime
from email.utils import parseaddr

from flask import redirect, request, url_for
from werkzeug.exceptions import BadRequest, NotFound

from pysite.base_route import RouteView
from pysite.constants import BotEventTypes, CHANNEL_JAM_LOGS
from pysite.decorators import csrf
from pysite.mixins import DBMixin, OAuthMixin, RMQMixin


class JamsJoinView(RouteView, DBMixin, OAuthMixin, RMQMixin):
    path = "/jams/join/<int:jam>"
    name = "jams.join"

    table_name = "code_jams"
    forms_table = "code_jam_forms"
    questions_table = "code_jam_questions"
    responses_table = "code_jam_responses"
    participants_table = "code_jam_participants"
    infractions_table = "code_jam_infractions"

    def get(self, jam):
        jam_obj = self.db.get(self.table_name, jam)

        if not jam_obj:
            return NotFound()

        if not self.user_data:
            return self.redirect_login(jam=jam)

        infractions = self.get_infractions(self.user_data["user_id"])

        for infraction in infractions:
            if infraction["number"] == -1:  # Indefinite ban
                return self.render("main/jams/banned.html", infraction=infraction, jam=jam_obj)

            if infraction["number"]:  # Got some jams left
                if jam not in infraction["decremented_for"]:
                    # Make sure they haven't already tried to apply for this jam
                    infraction["number"] -= 1
                    infraction["decremented_for"].append(jam)

                    self.db.insert(self.infractions_table, infraction, conflict="replace")

                return self.render("main/jams/banned.html", infraction=infraction, jam=jam_obj)

            if jam in infraction["decremented_for"]:
                # They already tried to apply for this jam
                return self.render("main/jams/banned.html", infraction=infraction, jam=jam_obj)

        participant = self.db.get(self.participants_table, self.user_data["user_id"])

        if not participant:
            return redirect(url_for("main.jams.profile", form=jam))

        if self.get_response(jam, self.user_data["user_id"]):
            return self.render("main/jams/already.html", jam=jam_obj)

        form_obj = self.db.get(self.forms_table, jam)
        questions = []

        if form_obj:
            for question in form_obj["questions"]:
                questions.append(self.db.get(self.questions_table, question))

        return self.render(
            "main/jams/join.html", jam=jam_obj, form=form_obj,
            questions=questions, question_ids=[q["id"] for q in questions]
        )

    @csrf
    def post(self, jam):
        jam_obj = self.db.get(self.table_name, jam)

        if not jam_obj:
            return NotFound()

        if not self.user_data:
            return self.redirect_login(jam=jam)

        infractions = self.get_infractions(self.user_data["user_id"])

        for infraction in infractions:
            if infraction["number"] == -1:  # Indefinite ban
                self.log_banned(infraction["number"], infraction["reason"])
                return self.render("main/jams/banned.html", infraction=infraction)

            if infraction["number"]:  # Got some jams left
                if jam not in infraction["decremented_for"]:
                    # Make sure they haven't already tried to apply for this jam
                    infraction["number"] -= 1
                    infraction["decremented_for"].append(jam)

                    self.db.insert(self.infractions_table, infraction, conflict="replace")

                self.log_banned(infraction["number"], infraction["reason"])
                return self.render("main/jams/banned.html", infraction=infraction, jam=jam_obj)

            if jam in infraction["decremented_for"]:
                # They already tried to apply for this jam
                self.log_banned(infraction["number"], infraction["reason"])
                return self.render("main/jams/banned.html", infraction=infraction, jam=jam_obj)

        participant = self.db.get(self.participants_table, self.user_data["user_id"])

        if not participant:
            return redirect(url_for("main.jams.profile"))

        if self.get_response(jam, self.user_data["user_id"]):
            return self.render("main/jams/already.html", jam=jam_obj)

        form_obj = self.db.get(self.forms_table, jam)

        if not form_obj:
            return NotFound()

        questions = []

        for question in form_obj["questions"]:
            questions.append(self.db.get(self.questions_table, question))

        answers = []

        for question in questions:
            value = request.form.get(question["id"])
            answer = {"question": question["id"]}

            if not question["optional"] and value is None:
                return BadRequest()

            if question["type"] == "checkbox":
                if value == "on":
                    answer["value"] = True
                elif not question["optional"]:
                    return BadRequest()
                else:
                    answer["value"] = False

            elif question["type"] == "email":
                if value:
                    address = parseaddr(value)

                    if address == ("", ""):
                        return BadRequest()

                answer["value"] = value

            elif question["type"] in ["number", "range", "slider"]:
                if value is not None:
                    value = int(value)

                    if value > int(question["data"]["max"]) or value < int(question["data"]["min"]):
                        return BadRequest()

                answer["value"] = value

            elif question["type"] == "radio":
                if value:
                    if value not in question["data"]["options"]:
                        return BadRequest()

                answer["value"] = value

            elif question["type"] in ["text", "textarea"]:
                answer["value"] = value

            answers.append(answer)

        user_id = self.user_data["user_id"]

        response = {
            "snowflake": user_id,
            "jam": jam,
            "approved": False,
            "answers": answers
        }

        self.db.insert(self.responses_table, response)
        self.log_success()

        return self.render("main/jams/thanks.html", jam=jam_obj)

    def get_response(self, jam, user_id):
        query = self.db.query(self.responses_table).filter({"jam": jam, "snowflake": user_id})
        result = self.db.run(query, coerce=list)

        if result:
            return result[0]
        return None

    def get_infractions(self, user_id):
        query = self.db.query(self.infractions_table).filter({"participant": user_id})
        return self.db.run(query, coerce=list)

    def log_banned(self, number, reason):
        user_data = self.user_data

        user_id = user_data["user_id"]
        username = user_data["username"]
        discriminator = user_data["discriminator"]

        message = f"Failed code jam signup from banned user: {user_id} ({username}#{discriminator})\n\n"

        if number == -1:
            message += f"This user has been banned indefinitely. Reason: '{reason}'"
        elif number < 1:
            message += f"This application has expired the infraction. Reason: '{reason}'"
        else:
            message += f"This user has {number} more applications left before they're unbanned. Reason: '{reason}'"

        self.rmq_bot_event(
            BotEventTypes.mod_log,
            {
                "level": "warning", "title": "Code Jams: Applications",
                "message": message
            }
        )

    def log_success(self):
        user_data = self.user_data

        user_id = user_data["user_id"]
        username = user_data["username"]
        discriminator = user_data["discriminator"]

        self.rmq_bot_event(
            BotEventTypes.mod_log,
            {
                "level": "info", "title": "Code Jams: Applications",
                "message": f"Successful code jam signup from user: {user_id} "
                           f"({username}#{discriminator})"
            }
        )

        self.rmq_bot_event(
            BotEventTypes.send_embed,
            {
                "target": CHANNEL_JAM_LOGS,
                "title": "Code Jams: Applications",
                "description": f"Successful code jam signup from user: {user_id} "
                               f"({username}#{discriminator})",
                "colour": 0x2ecc71,  # Green from d.py
                "timestamp": datetime.datetime.now().isoformat()
            }
        )
