import json
import secrets
from helphours import app, log, db, notifier, queue_handler, routes_helper, password_reset, stats, zoom_helper
from flask import render_template, url_for, redirect, request, g, send_from_directory
from helphours.forms import JoinQueueForm, RemoveSelfForm, InstructorForm
from helphours.student import Student
from flask_login import current_user, login_required
from helphours.models.instructor import Instructor
from helphours.models.visit import Visit
from helphours.models.zoom_link import ZoomLink
from datetime import datetime
from werkzeug.security import generate_password_hash

# Would likely need to be stored in a databse if we want multiple instances of this
# running
queue_is_open = False
current_zoom_link = ''
CURRENT_DUCK = '/static/images/duck.png'


@app.before_request
def inject_variables():
    g.user = current_user
    g.queue_is_open = queue_is_open


@app.context_processor
def course_info():
    return dict(COURSE_NAME=app.config['COURSE_NAME'],
                COURSE_DESCRIPTION=app.config['COURSE_DESCRIPTION'],
                CURRENT_DUCK=CURRENT_DUCK,
                NAV_COLOR=app.config['NAV_COLOR'],
                NAV_GRADIENT=app.config['NAV_GRADIENT'])


@app.route("/", methods=['GET'])
@app.route("/index", methods=['GET'])
def index():
    return render_template('index.html')


@app.route("/join", methods=['GET', 'POST'])
def join():
    form = JoinQueueForm()
    message = ""
    if not queue_is_open and request.method == 'POST':
        message = "Sorry, the queue was closed."
    else:
        # go to the page that shows the people in the queue if you've submitted
        # a valid form
        if form.validate_on_submit():
            visit = Visit(eid=form.eid.data, time_entered=datetime.utcnow(), time_left=None,
                          was_helped=0, instructor_id=None)
            db.session.add(visit)
            db.session.commit()
            s = Student(form.name.data, form.email.data,
                        form.eid.data, visit.id)
            place = queue_handler.enqueue(s)
            notifier.send_message(form.email.data,
                                  f"Notification from {app.config['COURSE_NAME']} Lab Hours Queue",
                                  render_template("email/added_to_queue_email.html",
                                                  view_link=app.config['WEBSITE_LINK'] + url_for('view'),
                                                  place_str=routes_helper.get_place_str(place),
                                                  student_name=form.name.data, remove_code=form.eid.data),
                                  'html')
            return redirect(url_for('view'))
        else:
            if len(form.errors) > 0:
                message = next(iter(form.errors.values()))[0]
    # render the template for submitting otherwise
    return render_template('join.html', form=form, queue_is_open=queue_is_open, message=message)


@app.route("/view", methods=['GET', 'POST'])
def view():
    global queue_is_open
    if request.method == "POST" and current_user.is_authenticated:
        queue_is_open = routes_helper.handle_line_form(request, queue_is_open)
    queue = queue_handler.get_students()
    return render_template('view.html', queue=queue, queue_is_open=queue_is_open)


@app.route("/line", methods=['GET', 'POST'])
def line():
    return redirect(url_for('view'))


@app.route("/remove", methods=['GET', 'POST'])
def remove():
    form = RemoveSelfForm()
    message = ""
    if request.method == 'POST':
        if form.validate_on_submit():
            # remove em
            s = queue_handler.remove_eid(form.eid.data)
            if s is not None:
                v = Visit.query.filter_by(id=s.id).first()
                if v is not None:
                    v.time_left = datetime.utcnow()
                    v.was_helped = 0
                    db.session.commit()
                    return redirect(url_for('view'))
                else:
                    # Serious issue, there are inconsistencies in the database.
                    log.error('Student in queue does not have a corresponding entry in visits table.', notify=True)
                    return render_template('message_error.html', title="Error Removing from queue",
                                           body="Sorry, something went wrong when trying to remove you from the queue.")
            else:
                message = "EID not found in queue"
        else:
            message = "EID is required"
    return render_template('remove.html', form=form, message=message)


@app.route("/zoom", methods=['GET'])
def zoom_links():
    return render_template('zoom.html', links=ZoomLink.query.all())


@app.route('/change_zoom', methods=['GET', 'POST'])
@login_required
def change_zoom():
    message = ""
    preset_links = ZoomLink.query.all()

    if request.method == 'POST':
        if 'cancel' in request.form:
            return redirect(url_for('zoom_links'))

        new_presets = request.form['preset-links']
        try:
            new_zoom_links = zoom_helper.parse_links(new_presets)
            ZoomLink.query.delete()
            for new_link in new_zoom_links:
                db.session.add(new_link)
            db.session.commit()
            log.info(f'{current_user.first_name} {current_user.last_name} updated the Zoom links.')
            return redirect(url_for('zoom_links'))
        except Exception as e:
            message = str(e)
    return render_template('edit_preset_links.html', message=message, preset_links=preset_links)


@app.route("/schedule", methods=['GET'])
def schedule_redirect():
    return redirect(app.config['SCHEDULE_LINK'])


@app.route('/admin_panel', methods=['GET', 'POST'])
@login_required
def admin_panel():
    if current_user.is_admin:
        return render_template('admin_panel.html', instructors=Instructor.query.all())
    else:
        return render_template('message_error.html', title="Admin Panel", body="Not authenticated, must be admin.")


@app.route('/edit_instructor', methods=['GET', 'POST'])
@login_required
def edit_instructor():
    if current_user.is_admin:
        if 'id' not in request.args:
            return render_template('message_error.html', title="Error", body="Missing instructor id")
        instr = Instructor.query.filter_by(id=request.args['id']).first()
        if request.method == 'GET':
            if instr is None:
                return render_template('message_error.html', title="Error", body="Invalid instructor id")
            form = InstructorForm(first_name=instr.first_name, last_name=instr.last_name, email=instr.email, is_active=(
                instr.is_active != 0), is_admin=(instr.is_admin != 0))
            return render_template('edit_instructor.html',
                                   title="Edit Instructor",
                                   form=form, message='', id=instr.id)
        else:
            if 'cancel' in request.form:
                return redirect('admin_panel')

            form = InstructorForm()
            if form.validate_on_submit():
                instr.first_name = form.first_name.data
                instr.last_name = form.last_name.data
                instr.email = form.email.data
                instr.is_active = 1 if form.is_active.data else 0
                instr.is_admin = 1 if form.is_admin.data else 0
                db.session.commit()
                log.info(f'The account for {instr.first_name} {instr.last_name} was updated.')
                return redirect('admin_panel')
            else:
                message = 'Enter a valid email address'
                return render_template('edit_instructor.html',
                                       title="Edit Instructor",
                                       form=form,
                                       message=message)
    else:
        return render_template('reset_message.html', title="Edit user", body="Not authenticated")


@app.route('/add_instructor', methods=['GET', 'POST'])
@login_required
def add_instructor():
    if current_user.is_admin:
        message = ''
        if request.method == 'GET':
            form = InstructorForm(is_active=True)
            return render_template('edit_instructor.html',
                                   title="Create Instructor", form=form,
                                   message=message)
        else:
            form = InstructorForm()
            if form.validate_on_submit():
                instr = Instructor()
                instr.first_name = form.first_name.data
                instr.last_name = form.last_name.data
                instr.email = form.email.data
                instr.is_active = 1 if form.is_active.data else 0
                instr.is_admin = 1 if form.is_admin.data else 0
                instr.password_hash = generate_password_hash(
                    secrets.token_urlsafe(20))
                db.session.add(instr)
                db.session.commit()
                password_reset.new_user(instr)
                log.info(f'New account created for {instr.first_name} {instr.last_name}.')
                return redirect('admin_panel')
            else:
                message = 'Enter a valid email address'
    else:
        return render_template('reset_message.html', title="Edit user",
                               body="Not authenticated")


@app.route('/stats', methods=['GET', 'POST'])
@login_required
def stats_page():
    if 'range' not in request.args:
        range = "all"
    else:
        range = request.args['range']
    return stats.get_graphs(range)


@app.route('/about', methods=['GET'])
def about_page():
    return render_template('about.html', title="About Our Creators")


@app.route('/clear', methods=['POST'])
def clear():
    if 'token' not in request.form:
        return json.dumps({'success': False}), 401, {'ContentType': 'application/json'}
    expected_token = app.config['CLEAR_TOKEN']
    if request.form['token'] != expected_token:
        return json.dumps({'success': False}), 401, {'ContentType': 'application/json'}
    for student in queue_handler.get_students():
        routes_helper.remove_helper(student.id)
    log.info('Queue was cleared through /clear route')
    return json.dumps({'success': True}), 200, {'ContentType': 'application/json'}


@app.route('/open', methods=['POST'])
def open():
    global queue_is_open
    global CURRENT_DUCK
    if 'token' not in request.form:
        return json.dumps({'success': False}), 401, {'ContentType': 'application/json'}
    expected_token = app.config['OPEN_TOKEN']
    if request.form['token'] != expected_token:
        return json.dumps({'success': False}), 401, {'ContentType': 'application/json'}
    queue_is_open = True
    CURRENT_DUCK = url_for('static', filename='images/duck.png')
    log.info('Queue was opened through /open route')
    return json.dumps({'success': True}), 200, {'ContentType': 'application/json'}


@app.route('/close', methods=['POST'])
def close():
    global queue_is_open
    global CURRENT_DUCK
    if 'token' not in request.form:
        return json.dumps({'success': False}), 401, {'ContentType': 'application/json'}
    expected_token = app.config['CLOSE_TOKEN']
    if request.form['token'] != expected_token:
        return json.dumps({'success': False}), 401, {'ContentType': 'application/json'}
    queue_is_open = False
    CURRENT_DUCK = url_for('static', filename='images/night-duck.png')
    log.info('Queue was closed through /close route')
    return json.dumps({'success': True}), 200, {'ContentType': 'application/json'}


@app.route('/robots.txt')
def robots_txt():
    return send_from_directory(app.static_folder, request.path[1:])


# noqa: F401 == Ignore rule about unused imports
from helphours import error_routes  # noqa: F401
from helphours import account_routes    # noqa: F401
from helphours import json_routes   # noqa: F401
