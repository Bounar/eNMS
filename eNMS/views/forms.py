from flask_wtf import FlaskForm
from wtforms import IntegerField, StringField

from eNMS.base.models import ObjectField


class ViewForm(FlaskForm):
    pools = ObjectField("Pool")


class GoogleEarthForm(FlaskForm):
    name = StringField()
    label_size = IntegerField(default=1)
    line_width = IntegerField(default=2)
